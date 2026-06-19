# Nextcloud Talk Platform Plugin for Hermes Agent

A Hermes Agent platform plugin that connects Nextcloud Talk via webhook. Based on the OpenClaw nextcloud-talk extension pattern.

## Features

- Receive Nextcloud Talk messages via HTTP POST webhook
- HMAC-SHA256 signature verification (timing attack resistant)
- ActivityPub-style payload parsing (Create/Update/Delete)
- Send messages via Bot API (reply-to support)
- Message deduplication (5 min TTL)
- `/healthz` health check endpoint
- Standalone sender for cron jobs
- **DDoS protection: IP-based Rate Limiting** (30 min block after 10 failures in 5 min)
- **Room-level permission policies**: group member restriction, DM allow-list
- **Config validation**: automatic port range and URL format validation

## Installation

```bash
pip install aiohttp httpx
```

Place in Hermes Agent's `plugins/platforms/` directory:

```
~/.hermes/plugins/platforms/nextcloud_talk/
├── plugin.yaml
├── __init__.py
└── adapter.py
```

## Configuration (config.yaml)

```yaml
gateway:
  platforms:
    nextcloud_talk:
      enabled: true
      extra:
        base_url: "https://your-nextcloud.example.com"
        bot_secret: "your-bot-secret"
        host: "0.0.0.0"
        port: 8745
        path: "/nextcloud-talk/callback"
        # ── Optional: Permission policies ──
        # group_policy: "all"        # "all" | "members" | "mentioned"
        # dm_policy: "all"           # "all" | "restricted"
        # allowed_users: ["user1", "user2"]
        # allowed_dm_users: ["user1"]
```

Or via environment variables (config.yaml takes precedence):

```bash
export NEXTCLOUD_TALK_BASE_URL=https://your-nextcloud.example.com
export NEXTCLOUD_TALK_BOT_SECRET=your-bot-secret
export NEXTCLOUD_TALK_PORT=8745
export NEXTCLOUD_TALK_PATH=/nextcloud-talk/callback
# ── Optional: Permission policies ──
export NEXTCLOUD_TALK_ALLOWED_USERS=user1,user2,user3
export NEXTCLOUD_TALK_GROUP_POLICY=members     # "all" | "members" | "mentioned"
export NEXTCLOUD_TALK_DM_POLICY=restricted     # "all" | "restricted"
export NEXTCLOUD_TALK_ALLOWED_DM_USERS=user1,user2
```

### Permission Policies

| Setting | Options | Description |
|---------|---------|-------------|
| `group_policy` | `all` (default) | Allow all messages in group chats |
| | `members` | Only allow users in `allowed_users` (empty list = deny all) |
| | `mentioned` | Only allow messages mentioning the bot (future) |
| `dm_policy` | `all` (default) | Allow all DMs |
| | `restricted` | Only allow users in `allowed_dm_users` (empty list = deny all) |

## Nextcloud Talk Webhook Setup

1. Open Nextcloud Talk dashboard
2. Go to **Settings → Bot**
3. Click **Create Bot**
4. Set bot name (e.g., "Hermes Agent")
5. Copy **HMAC Secret** — needed for configuration

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/nextcloud-talk/callback` | Message webhook |
| GET | `/healthz` | Health check |

## Response Codes

| Code | Description |
|------|-------------|
| 200 | Message received/accepted |
| 400 | Bad request (missing headers, JSON error) |
| 403 | Signature verification failed |
| 429 | Rate limit exceeded (10+ failures) |
| 500 | Internal server error |

## Architecture

```
Nextcloud Talk ──POST──> [Hermes Plugin] ──> [Agent Processing] ──> Response
                            │
                            ├─ HMAC-SHA256 verification
                            ├─ Rate Limiting (IP-based)
                            ├─ ActivityPub parsing (Create/Update/Delete)
                            ├─ Dedup (5min TTL)
                            ├─ Permission policy check (group/dm)
                            └─ Bot API send (reply-to support)
```

## Testing

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

See [MANUAL_TEST_GUIDE.md](MANUAL_TEST_GUIDE.md) for detailed test instructions.

See [README_ko.md](README_ko.md) for Korean documentation.

## License

MIT