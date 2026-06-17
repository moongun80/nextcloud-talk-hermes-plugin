# Nextcloud Talk Platform Plugin for Hermes Agent

Hermes Agent용 Nextcloud Talk webhook 플랫폼 플러그인. OpenClaw의 nextcloud-talk extension 패턴을 참고하여 구현.

## 기능

- HTTP POST webhook으로 Nextcloud Talk 메시지 수신
- HMAC-SHA256 서명 검증 (Timing attack 방지)
- ActivityPub 스타일 페이로드 파싱
- Bot API를 통한 메시지 발송 (reply-to 지원)
- 메시지 중복 방지 (5분 TTL)
- `/healthz` 헬스체크 엔드포인트
- Cron jobs용 standalone sender

## 설치

```bash
pip install aiohttp httpx
```

Hermes Agent의 `plugins/platforms/` 디렉토리에 배치:

```
~/.hermes/hermes-agent/plugins/platforms/nextcloud_talk/
├── plugin.yaml
├── __init__.py
└── adapter.py
```

## 설정 (config.yaml)

```yaml
gateway:
  platforms:
    nextcloud_talk:
      enabled: true
      extra:
        base_url: "https://your-nextcloud.example.com"
        bot_token: "your-bot-access-token"
        bot_secret: "your-bot-secret"
        host: "0.0.0.0"
        port: 8745
        path: "/nextcloud-talk/callback"
```

또는 환경변수 (config.yaml 우선):

```bash
export NEXTCLOUD_TALK_BASE_URL=https://your-nextcloud.example.com
export NEXTCLOUD_TALK_BOT_TOKEN=your-bot-token
export NEXTCLOUD_TALK_BOT_SECRET=your-bot-secret
export NEXTCLOUD_TALK_PORT=8745
export NEXTCLOUD_TALK_PATH=/nextcloud-talk/callback
```

## Nextcloud Talk Webhook 설정

1. Nextcloud Talk 대시보드에서 Bot 설정
2. Webhook URL: `http://<서버IP>:8745/nextcloud-talk/callback`
3. Bot Access Token 및 HMAC Secret 발급

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| POST | `/nextcloud-talk/callback` | 메시지 webhook |
| GET | `/healthz` | 헬스체크 |

## 아키텍처

```
Nextcloud Talk ──POST──> [Hermes Plugin] ──> [Agent Processing] ──> Response
                            │
                            ├─ HMAC-SHA256 검증
                            ├─ ActivityPub 파싱 (Create only)
                            ├─ Dedup (5min TTL)
                            └─ Bot API 발송 (reply-to 지원)
```

## 라이선스

MIT