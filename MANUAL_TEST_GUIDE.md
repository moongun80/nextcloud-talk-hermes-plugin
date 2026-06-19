# Nextcloud Talk Hermes Plugin — Manual Test Guide

## Table of Contents

1. [Local Testing](#local-testing)
2. [Step-by-Step Nextcloud Talk Integration](#step-by-step-nextcloud-talk-integration)
3. [Debugging Tips](#debugging-tips)

---

## Local Testing

### 1. Install Dependencies

```bash
cd /home/mg/Project/nextcloud-talk-hermes-plugin
pip install aiohttp httpx pytest pytest-asyncio
```

### 2. Run Unit Tests

```bash
pytest tests/test_adapter.py -v
```

Expected: All tests pass (PASS)

### 3. Run Integration Tests

```bash
pytest tests/test_integration.py -v
```

Expected: All tests pass (PASS)

### 4. Run All Tests

```bash
pytest tests/ -v
```

---

## curl Examples — Manual Webhook Testing

### Signature Generation Script

```bash
#!/bin/bash
# sign.sh — HMAC signature generation script

SECRET="test-secret"
PAYLOAD='{"type":"Create","actor":{"type":"Person","id":"u1","name":"Test"},"object":{"type":"Note","id":"msg-1","content":"Hello!","mediaType":"text/plain"},"target":{"type":"Collection","id":"room-1","name":"room-1"},"isGroupChat":false}'

RANDOM_VAL=$(python3 -c "import secrets; print(secrets.token_hex(16))")
SIGNATURE=$(echo -n "${RANDOM_VAL}${PAYLOAD}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

curl -X POST http://127.0.0.1:8745/nextcloud-talk/callback \
  -H "Content-Type: application/json" \
  -H "x-nextcloud-talk-signature: ${SIGNATURE}" \
  -H "x-nextcloud-talk-random: ${RANDOM_VAL}" \
  -d "${PAYLOAD}"
```

Run:

```bash
chmod +x sign.sh
./sign.sh
# Expected: {"status":"accepted"}
```

### Invalid Signature Test

```bash
curl -X POST http://127.0.0.1:8745/nextcloud-talk/callback \
  -H "Content-Type: application/json" \
  -H "x-nextcloud-talk-signature: bad-signature" \
  -H "x-nextcloud-talk-random: abc123" \
  -d '{"type":"Create","actor":{"type":"Person","id":"u1","name":"Test"},"object":{"type":"Note","id":"msg-1","content":"Hello!"},"target":{"type":"Collection","id":"room-1","name":"room-1"}}'
# Expected: {"error":"Invalid signature"} (HTTP 403)
```

### Rate Limiting Test

```bash
# Send 10+ requests with invalid signatures
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "Request $i: HTTP %{http_code}\n" \
    -X POST http://127.0.0.1:8745/nextcloud-talk/callback \
    -H "Content-Type: application/json" \
    -H "x-nextcloud-talk-signature: bad-$i" \
    -H "x-nextcloud-talk-random: abc123" \
    -d '{"type":"Create","actor":{"type":"Person","id":"u1","name":"Test"},"object":{"type":"Note","id":"msg-1","content":"Hello!"},"target":{"type":"Collection","id":"room-1","name":"room-1"}}'
done
# Expected: Request 1-10: HTTP 403, Request 11: HTTP 429
```

---

## Step-by-Step Nextcloud Talk Integration

### Step 1: Create Bot Account in Nextcloud

1. Open Nextcloud dashboard
2. Go to **Settings → Bot**
3. Click **Create Bot**
4. Set bot name (e.g., "Hermes Agent")
5. Copy **Access Token** and **HMAC Secret** — needed later

### Step 2: Configure Hermes Plugin

Set via `config.yaml` or environment variables:

```yaml
gateway:
  platforms:
    nextcloud_talk:
      enabled: true
      extra:
        base_url: "https://your-nextcloud.example.com"
        bot_secret: "your-hmac-secret-here"
        host: "0.0.0.0"
        port: 8745
        path: "/nextcloud-talk/callback"
```

### Step 3: Register Webhook URL

1. In Nextcloud Bot settings page, enter **Webhook URL**
2. URL format: `http://<server-ip>:8745/nextcloud-talk/callback`
3. Save

### Step 4: Connection Test

```bash
curl http://127.0.0.1:8745/healthz
# Expected: {"status":"healthy"}
```

### Step 5: Real Message Test

Send a message in a Nextcloud Talk chat with the bot → Hermes Agent receives and processes it.

---

## Debugging Tips

### 1. Adjust Logging Level

```python
import logging
logging.getLogger("plugins.platforms.nextcloud_talk").setLevel(logging.DEBUG)
```

### 2. Common Issues

| Symptom | Cause | Solution |
|---------|-------|----------|
| `403 Invalid signature` | Bot secret mismatch | Verify `bot_secret` in config.yaml matches Nextcloud Bot settings |
| `400 Missing signature/random headers` | Missing headers | Enable webhook headers in Nextcloud Bot settings |
| `Bad JSON` | Payload format error | Ensure ActivityPub format compliance |
| Connection failure | Port conflict | Change to different port (`NEXTCLOUD_TALK_PORT`) |

### 3. Rate Limiting Related

- Look for `Rate limited:` message in logs
- 30 min wait required for unblock
- Nextcloud server IP is recorded as client IP

### 4. Permission Policy Related

- Look for `Permission denied:` message in logs
- Verify `allowed_users`, `allowed_dm_users` match user IDs
- Restart adapter after policy changes

### 5. Test Debug Script

```python
#!/usr/bin/env python3
"""debug_webhook.py — Quick webhook signature tester."""

import hashlib
import hmac
import json
import secrets

SECRET = "test-secret"
BASE_URL = "http://127.0.0.1:8745"
PATH = "/nextcloud-talk/callback"

payload = {
    "type": "Create",
    "actor": {"type": "Person", "id": "debug-user", "name": "Debug"},
    "object": {
        "type": "Note",
        "id": "debug-msg-001",
        "content": "Debug message from script",
        "mediaType": "text/plain",
    },
    "target": {"type": "Collection", "id": "debug-room", "name": "debug-room"},
}

random_val = secrets.token_hex(16)
body = json.dumps(payload)
sig = hmac.new(
    SECRET.encode(),
    (random_val + body).encode(),
    hashlib.sha256,
).hexdigest()

import urllib.request
req = urllib.request.Request(
    f"{BASE_URL}{PATH}",
    data=body.encode(),
    headers={
        "Content-Type": "application/json",
        "x-nextcloud-talk-signature": sig,
        "x-nextcloud-talk-random": random_val,
    },
    method="POST",
)
resp = urllib.request.urlopen(req)
print(f"Status: {resp.status}")
print(f"Body: {resp.read().decode()}")
```

---

## Test Results Checklist

- [ ] `pytest tests/test_adapter.py -v` — All PASS
- [ ] `pytest tests/test_integration.py -v` — All PASS
- [ ] curl valid webhook → `{"status":"accepted"}`
- [ ] curl invalid signature → `{"error":"Invalid signature"}` (403)
- [ ] curl rate limit → `{"error":"Too many failed attempts"}` (429)
- [ ] curl permission denied → `{"status":"permission_denied"}` (200)
- [ ] `/healthz` → `{"status":"healthy"}` (200)