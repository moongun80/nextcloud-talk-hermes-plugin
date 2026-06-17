# Hermes Agent 용 Nextcloud Talk 플랫폼 플러그인

Hermes Agent 의 Nextcloud Talk webhook 플랫폼 플러그인. OpenClaw 의 nextcloud-talk extension 패턴을 참고하여 구현.

## 기능

- HTTP POST webhook 으로 Nextcloud Talk 메시지 수신
- HMAC-SHA256 서명 검증 (Timing attack 방지)
- ActivityPub 스타일 페이로드 파싱 (Create/Update/Delete 지원)
- Bot API 를 통한 메시지 발송 (reply-to 지원)
- 메시지 중복 방지 (5 분 TTL)
- `/healthz` 헬스체크 엔드포인트
- Cron jobs 용 standalone sender
- **DDoS 방어: IP 기반 Rate Limiting** (5 분 내 10 회 실패 시 30 분 차단)
- **Room 별 권한 정책**: 그룹 채팅 멤버 제한, DM 허용 사용자 제한
- **설정 검증**: 포트 범위, URL 형식 자동 검증

## 설치

```bash
pip install aiohttp httpx
```

Hermes Agent 의 `plugins/platforms/` 디렉토리에 배치:

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
        # ── Optional: Permission policies ──
        # group_policy: "all"        # "all" | "members" | "mentioned"
        # dm_policy: "all"           # "all" | "restricted"
        # allowed_users: ["user1", "user2"]
        # allowed_dm_users: ["user1"]
```

또는 환경변수 (config.yaml 우선):

```bash
export NEXTCLOUD_TALK_BASE_URL=https://your-nextcloud.example.com
export NEXTCLOUD_TALK_BOT_TOKEN=your-bot-token
export NEXTCLOUD_TALK_BOT_SECRET=your-bot-secret
export NEXTCLOUD_TALK_PORT=8745
export NEXTCLOUD_TALK_PATH=/nextcloud-talk/callback
# ── Optional: Permission policies ──
export NEXTCLOUD_TALK_ALLOWED_USERS=user1,user2,user3
export NEXTCLOUD_TALK_GROUP_POLICY=members     # "all" | "members" | "mentioned"
export NEXTCLOUD_TALK_DM_POLICY=restricted     # "all" | "restricted"
export NEXTCLOUD_TALK_ALLOWED_DM_USERS=user1,user2
```

### 권한 정책 설명

| 설정 | 옵션 | 설명 |
|------|------|------|
| `group_policy` | `all` (기본) | 그룹 채팅의 모든 메시지 허용 |
| | `members` | `allowed_users` 에 있는 사용자만 허용 |
| | `mentioned` | 봇이 멘션된 메시지만 허용 (향후 지원) |
| `dm_policy` | `all` (기본) | 모든 DM 허용 |
| | `restricted` | `allowed_dm_users` 에 있는 사용자만 허용 |

## Nextcloud Talk Webhook 설정

1. Nextcloud Talk 대시보드에서 Bot 설정
2. Webhook URL: `http://<서버 IP>:8745/nextcloud-talk/callback`
3. Bot Access Token 및 HMAC Secret 발급

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| POST | `/nextcloud-talk/callback` | 메시지 webhook |
| GET | `/healthz` | 헬스체크 |

## 응답 코드

| 코드 | 설명 |
|------|------|
| 200 | 메시지 수신/수락됨 |
| 400 | 잘못된 요청 (헤더 누락, JSON 오류) |
| 403 | 서명 검증 실패 |
| 429 | Rate limit 초과 (10 회 이상 실패) |
| 500 | 내부 서버 오류 |

## 아키텍처

```
Nextcloud Talk ──POST──> [Hermes Plugin] ──> [Agent Processing] ──> Response
                            │
                            ├─ HMAC-SHA256 검증
                            ├─ Rate Limiting (IP 기반)
                            ├─ ActivityPub 파싱 (Create/Update/Delete)
                            ├─ Dedup (5min TTL)
                            ├─ 권한 정책 체크 (group/dm)
                            └─ Bot API 발송 (reply-to 지원)
```

## 테스트

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

자세한 테스트 가이드는 [MANUAL_TEST_GUIDE.md](MANUAL_TEST_GUIDE.md) 참조.

See [README.md](README.md) for English documentation.

## 라이선스

MIT