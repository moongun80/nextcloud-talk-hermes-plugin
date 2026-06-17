# Nextcloud Talk Hermes Plugin — 수동 테스트 가이드

## 목차

1. [로컬 테스트](#로컬-테스트)
2. [Nextcloud Talk 연동 단계별 가이드](#nextcloud-talk-연동-단계별-가이드)
3. [디버깅 팁](#디버깅-팁)

---

## 로컬 테스트

### 1. 의존성 설치

```bash
cd /home/mg/Project/nextcloud-talk-hermes-plugin
pip install aiohttp httpx pytest pytest-asyncio
```

### 2. Unit 테스트 실행

```bash
pytest tests/test_adapter.py -v
```

예상 결과: 모든 테스트 통과 (PASS)

### 3. Integration 테스트 실행

```bash
pytest tests/test_integration.py -v
```

예상 결과: 모든 테스트 통과 (PASS)

### 4. 전체 테스트

```bash
pytest tests/ -v
```

---

## curl 예제 — 수동 webhook 테스트

### 서명 생성 스크립트

```bash
#!/bin/bash
# sign.sh — HMAC 서명 생성 스크립트

SECRET="test...hon3 -c "import secrets; print(secrets.token_hex(16))")
SIGNATURE=$(echo -n "${RANDOM_VAL}${PAYLOAD}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

curl -X POST http://127.0.0.1:8745/nextcloud-talk/callback \
  -H "Content-Type: application/json" \
  -H "x-nextcloud-talk-signature: ${SIGNATURE}" \
  -H "x-nextcloud-talk-random: ${RANDOM_VAL}" \
  -d "${PAYLOAD}"
```

실행:

```bash
chmod +x sign.sh
./sign.sh
# Expected: {"status":"accepted"}
```

### 유효하지 않은 서명 테스트

```bash
curl -X POST http://127.0.0.1:8745/nextcloud-talk/callback \
  -H "Content-Type: application/json" \
  -H "x-nextcloud-talk-signature: bad-signature" \
  -H "x-nextcloud-talk-random: abc123" \
  -d '{"type":"Create","actor":{"type":"Person","id":"u1","name":"Test"},"object":{"type":"Note","id":"msg-1","content":"Hello!"},"target":{"type":"Collection","id":"room-1","name":"room-1"}}'
# Expected: {"error":"Invalid signature"} (HTTP 403)
```

### Rate Limiting 테스트

```bash
# 10 번 이상 잘못된 서명으로 반복 요청
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

## Nextcloud Talk 연동 단계별 가이드

### Step 1: Nextcloud 에서 Bot 계정 생성

1. Nextcloud 대시보드 열기
2. **설정 → Bot** 메뉴로 이동
3. **Bot 생성** 버튼 클릭
4. Bot 이름 설정 (예: "Hermes Agent")
5. **Access Token** 및 **HMAC Secret** 복사 — 이 정보는 나중에 필요

### Step 2: Hermes Plugin 설정

`config.yaml` 또는 환경변수로 설정:

```yaml
gateway:
  platforms:
    nextcloud_talk:
      enabled: true
      extra:
        base_url: "https://your-nextcloud.example.com"
        bot_token: "your-access-token-here"
        bot_secret: "your-hmac-secret-here"
        host: "0.0.0.0"
        port: 8745
        path: "/nextcloud-talk/callback"
```

### Step 3: Webhook URL 등록

1. Nextcloud Bot 설정 페이지에서 **Webhook URL** 입력
2. URL 형식: `http://<서버 IP>:8745/nextcloud-talk/callback`
3. 저장

### Step 4: 연결 테스트

```bash
curl http://127.0.0.1:8745/healthz
# Expected: {"status":"healthy"}
```

### Step 5: 실제 메시지 테스트

Nextcloud Talk 에서 봇이 포함된 채팅방에 메시지 전송 → Hermes Agent 가 수신 확인

---

## 디버깅 팁

### 1. 로깅 레벨 조정

```python
import logging
logging.getLogger("plugins.platforms.nextcloud_talk").setLevel(logging.DEBUG)
```

### 2. common 문제들

| 증상 | 원인 | 해결책 |
|------|------|--------|
| `403 Invalid signature` | Bot secret 불일치 | config.yaml 의 `bot_secret` 과 Nextcloud Bot 설정 확인 |
| `400 Missing signature/random headers` | 헤더 누락 | Nextcloud Bot 설정에서 webhook 헤더 활성화 |
| `Bad JSON` | 페이로드 형식 오류 | ActivityPub 형식 준수 확인 |
| 연결 실패 | 포트 충돌 | 다른 포트 (`NEXTCLOUD_TALK_PORT`) 로 변경 |

### 3. Rate Limiting 관련

- 로그에서 `Rate limited:` 메시지 확인
- 차단 해제까지 30 분 대기 필요
- Nextcloud 서버 IP 가 클라이언트 IP 로 기록됨

### 4. Permission Policy 관련

- 로그에서 `Permission denied:` 메시지 확인
- `allowed_users`, `allowed_dm_users` 값이 user ID 와 일치하는지 확인
- policy 변경 후 adapter 재시작 필요

### 5. 테스트용 디버깅 스크립트

```python
#!/usr/bin/env python3
"""debug_webhook.py — Quick webhook signature tester."""

import hashlib
import hmac
import json
import secrets

SECRET = "test..._URL = "http://127.0.0.1:8745"
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

## 테스트 결과 체크리스트

- [ ] `pytest tests/test_adapter.py -v` — 모든 PASS
- [ ] `pytest tests/test_integration.py -v` — 모든 PASS
- [ ] curl 로 valid webhook → `{"status":"accepted"}`
- [ ] curl 로 invalid signature → `{"error":"Invalid signature"}` (403)
- [ ] curl 로 rate limit → `{"error":"Too many failed attempts"}` (429)
- [ ] curl 로 permission denied → `{"status":"permission_denied"}` (200)
- [ ] `/healthz` → `{"status":"healthy"}` (200)