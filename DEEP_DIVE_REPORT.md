# Nextcloud Talk Bot — 심층 조사 보고서

> 조사일: 2026-06-20  
> 대상: Nextcloud Talk Bot API + Hermes Agent Platform Plugin  
> 소스: 공식 문서 (nextcloud-talk.readthedocs.io), 기존 코드베이스, 테스트

---

## 1. 아키텍처 개요

```
┌──────────────┐     HTTP POST (ActivityPub JSON)      ┌──────────────────┐
│  Nextcloud   │ ──────────────────────────────────────▶│  Hermes Plugin   │
│   Talk Server│                                        │  (aiohttp server)│
│              │ ◀── HTTP POST (Bot API + HMAC sign) ───│                  │
└──────────────┘                                        └────────┬─────────┘
                                                                │
                                                         ┌──────▼──────┐
                                                         │ Hermes Agent│
                                                         │   Gateway   │
                                                         └─────────────┘
```

**핵심 원리**: 양방향 HMAC-SHA256 서명 기반 인증. 토큰 없음, 시크릿 기반.

---

## 2. Nextcloud Talk Bot API — 공식 스펙

### 2.1 기본 엔드포인트

```
Base: /ocs/v2.php/apps/spreed/api/v1
Capability: bots-v1 (Nextcloud 27.1+/Talk 17.1+)
```

봇은 **CLI 전용**으로 설치 가능:
```bash
./occ talk:bot:install --help
```

### 2.2 서명/검증 방식 (양방향 동일)

```
Signature = HMAC-SHA256(RANDOM_header + body, shared_secret)
```

| 방향 | 헤더 | 설명 |
|------|------|------|
| **Receive** (NC → bot) | `x-nextcloud-talk-signature` | HMAC-SHA256 서명 |
| | `x-nextcloud-talk-random` | 서명용 랜덤값 (64자) |
| | `x-nextcloud-talk-backend` | Nextcloud 서버 URL |
| **Send** (bot → NC) | `x-nextcloud-talk-bot-signature` | HMAC-SHA256 서명 (**-Bot-** 접미사 주의!) |
| | `x-nextcloud-talk-bot-random` | 랜덤값 |

⚠️ **주의**: Send 방향 헤더에 `-Bot-`가 반드시 들어가야 함.receive용 헤더를 쓰면 401 Unauthenticated.

### 2.3 메시지 수신 (Inbound) — ActivityPub 형식

```json
{
  "type": "Create",
  "actor": {
    "type": "Person",
    "id": "users/ada-lovelace",
    "name": "Ada Lovelace",
    "talkParticipantType": 1  // Optional (Talk 21+)
  },
  "object": {
    "type": "Note",
    "id": "1567",
    "name": "message",
    "content": "{\"message\":\"hi {mention-call1} !\",\"parameters\":{\"mention-call1\":{\"type\":\"call\",\"id\":\"n3xtc10ud\",\"name\":\"world\",\"call-type\":\"group\",\"icon-url\":\"...\"}}}",
    "mediaType": "text/markdown",
    "inReplyTo": {...}  // Optional (Talk 21+)
  },
  "target": {
    "type": "Collection",
    "id": "n3xtc10ud",  // ← room token (URL 아님!)
    "name": "world"
  }
}
```

**중요 필드**:
- `actor.id`: `users/{user_id}`, `guests/{hash}`, `emails/{email}`, `bots/{bot_sha1}`
- `object.content`: **JSON 인코딩된 딕셔너리** (`{"message": "...", "parameters": {...}}`)
- `target.id`: room token (URL 아님 — 직접 token)
- `isGroupChat`: boolean (그룹/DM 판별용)

### 2.4 메시지 전송 (Outbound)

```
POST /ocs/v2.php/apps/spreed/api/v1/bot/{room_token}/message
```

**Payload**:
```json
{
  "message": "Hello!",
  "replyTo": 1234,       // Optional: integer (message ID)
  "referenceId": "sha256...",  // Optional: 고유 식별자
  "silent": false        // Optional: 알림 안 보내기
}
```

**Response**:
| Status | 의미 |
|--------|------|
| 201 Created | 성공 |
| 400 Bad Request | replyTo 무효 또는 메시지 비어있음 |
| 401 Unauthenticated | 봇 검증 실패 |
| 404 Not Found | 봇이 해당 방에 없음 |
| 413 Payload Too Large | 메시지 초과 (32000자, 1000자까지 NC 16.0.1 이하) |

### 2.5 추가 이벤트 (Talk 21+)

| 이벤트 | type 값 | 설명 |
|--------|---------|------|
| Reaction Added | `Like` | 이모지 리액션 추가 |
| Reaction Removed | `Undo` | 이모지 리액션 제거 |
| Bot Added | `Join` | 봇이 방에 추가됨 |
| Bot Removed | `Leave` | 봇이 방에서 제거됨 |
| Message Delete | `Delete` | 메시지 삭제 |
| Message Update | `Update` | 메시지 수정 |

---

## 3. Hermes Plugin — 현재 구현 분석

### 3.1 파일 구조

```
plugins/platforms/nextcloud_talk/
├── plugin.yaml          # 메타데이터 + 환경변수 정의
├── __init__.py          # register export
└── adapter.py           # 메인 어댑터 (949줄)
```

### 3.2 핵심 클래스: `NextcloudTalkAdapter`

| 메서드 | 역할 |
|--------|------|
| `connect()` | aiohttp 웹서버 시작, webhook 라우트 등록 |
| `disconnect()` | 서버 중지, 리소스 정리 |
| `_handle_webhook()` | POST webhook 핸들러 (서명검증 → 파싱 → 권한체크 → dedup) |
| `send()` | Bot API 호출 (서명부착 → POST → message_id 추출) |
| `_parse_message()` | ActivityPub JSON → MessageEvent 변환 |
| `_parse_content()` | JSON-encoded content → plain text (placeholder 렌더링) |
| `_check_permissions()` | 그룹/DM 정책 기반 접근제어 |
| `_verify_signature()` | HMAC-SHA256 검증 (timing-safe) |
| `_is_duplicate()` | 메시지 중복검사 (5분 TTL) |
| `_is_replayed_random()` | replay 공격 방지 |
| `_is_rate_limited()` | IP 기반 rate limiting (10회/5분 → 30분 차단) |

### 3.3 Standalone Sender (크론용)

`_standalone_send()` 함수 — 게이트웨이 없이 별도 프로세스에서 Bot API 호출. 크론 결과 전달용.

### 3.4 설정 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `base_url` | (필수) | Nextcloud 서버 URL |
| `bot_secret` | (필수) | HMAC 시크릿 |
| `host` | `0.0.0.0` | 리슨 호스트 |
| `port` | `8745` | 리슨 포트 |
| `path` | `/nextcloud-talk/callback` | webhook 경로 |
| `max_message_length` | `32000` | 최대 메시지 길이 |
| `group_policy` | `all` | 그룹 정책 (`all`/`members`) |
| `dm_policy` | `all` | DM 정책 (`all`/`restricted`) |
| `allowed_users` | — | 그룹 허용 사용자 목록 |
| `allowed_dm_users` | — | DM 허용 사용자 목록 |
| `trusted_proxies` | (비어있음) | 프록시 신뢰 CIDR |

---

## 4. 구현 상세 — 핵심 로직

### 4.1 서명 검증 (Receive 방향)

```python
# NC → bot: "random + body"를 시크릿으로 HMAC-SHA256
computed = hmac.new(secret.encode(), (random_val + body).encode(), hashlib.sha256).hexdigest()
hmac.compare_digest(computed.lower(), signature.lower())  # timing-safe
```

### 4.2 서명 생성 (Send 방향)

```python
# bot → NC: 동일한 방식이지만 헤더에 -Bot- 접미사
random_value = secrets.token_hex(16)
signing_input = random_value + payload_body
signature = hmac.new(bot_secret.encode(), signing_input.encode(), hashlib.sha256).hexdigest()
# Headers: x-nextcloud-talk-bot-random, x-nextcloud-talk-bot-signature
```

### 4.3 메시지 파싱 — JSON-in-JSON

```python
# object.content는 JSON 인코딩됨:
raw = '{"message":"hi {mention-call1} !","parameters":{"mention-call1":{...}}}'
parsed = json.loads(raw)
message = parsed["message"]  # "hi {mention-call1} !"
params = parsed["parameters"]
# Placeholder 치환: "{mention-call1}" → "world"
```

### 4.4 Deduplication & Replay Protection

| 기법 | TTL | 키 |
|------|-----|-----|
| 메시지 dedup | 5분 | `message_id` (object.id) |
| Replay 방지 | 5분 | `random_val` (헤더 값) |

두 기법은 독립적으로 동작 — dedup TTL이 지나도 replay random은 별도 관리.

### 4.5 Rate Limiting

```
10회 실패 / 5분 윈도우 → 30분 IP 차단
성공 시 카운터 초기화
```

---

## 5. 테스트 커버리지

테스트 파일: `tests/test_adapter.py` (546줄)

| 테스트 클래스 | 항목 |
|--------------|------|
| `TestVerifySignature` | 유효/무효 서명, 공백, 대소문자 구분 |
| `TestIsDuplicate` | 첫 호출, 중복, 다른 ID, 빈 ID, 만료 에비iction |
| `TestValidatePort` | 유효/무효 포트 범위 |
| `TestValidateBaseUrl` | http/https 유효성 |
| `TestParseMessage` | Create/Update/Delete, 그룹/DM, 빈 콘텐츠 |
| `TestRateLimiting` | 카운팅, 임계치, 클리어, IP 독립성 |
| `TestPermissionPolicies` | group/dm 정책, empty whitelist, env var |
| `TestReplyToIntConversion` | replyTo 정수 변환 |
| `TestContentParsing` | JSON content, placeholder 치환, plain fallback |
| `TestSendFlow` | 전체 send 흐름, 서명 생성, 메시지 ID 추출 |

---

## 6. 알려진 문제점 및 개선 포인트

### 6.1 현재 구현되지 않은 기능

| 기능 | 상태 | 설명 |
|------|------|------|
| **Reaction 처리** | ❌ 미구현 | Talk 21+의 Like/Undo 이벤트 미처리 |
| **Typing indicator** | ❌ 구현했으나 pass | NC Talk엔 없음 |
| **Chat info 조회** | ⚠️ 제한적 | 단순히 chat_id 반환만 |
| **Mention 감지** | ⚠️ 부분적 | `mentioned` group_policyFuture |
| **Attachment 처리** | ❌ 미구현 | 파일/이미지 첨부 메시지 미지원 |
| **Thread/Reply 처리** | ⚠️ 부분적 | replyTo는 지원하지만 inReplyTo 파싱 미비 |
| **Guest/Email participant** | ⚠️ 부분적 | user_id 파싱은 되지만 guest hash 처리 미검증 |

### 6.2 잠재적 버그

1. **`_parse_content` fallback**: JSON 파싱 실패 시 원문을 그대로 반환 — `mediaType: text/markdown`일 때 마크다운 렌더링 고려 안함
2. **`target.id` URL vs Token**: 문서상 `target.id`는 URL이지만 실제 webhook에서는 token 직접 제공 — 코드에서 이미 token으로 처리 중 (M2 FIX)
3. **`object.name` 버그**: Talk 23 이전에는 attachment 메시지에서 `object.name`이 빈 문자열 — 코드에서 "message"로 fallback

---

## 7. OpenClaw 비교

Hermes plugin은 OpenClaw의 nextcloud-talk extension 패턴을 참고하여 구현됨. 주요 차이:

| 항목 | OpenClaw | Hermes |
|------|----------|--------|
| 인증 | HMAC-SHA256 | HMAC-SHA256 (동일) |
| 프레임워크 | Express.js | aiohttp (Python) |
| Dedup | 있음 | 있음 (5분) |
| Rate Limiting | 있음 | 있음 (IP 기반) |
| Permission Policy | — | 있음 (group/dm) |
| Standalone Sender | — | 있음 (크론용) |
| Reaction 처리 | 부분적 | 미구현 |

---

## 8. Nextcloud Talk 버전별 변화

| 버전 | 릴리즈일 | 주요 변화 |
|------|----------|-----------|
| NC 27.1 / Talk 17.1 | 2023-09 | 봇/웹훅 최초 도입 |
| NC 31 / Talk 21 | 2025-02 | Reaction, inReplyTo, talkParticipantType 추가 |
| NC 33 / Talk 23 | 2026-02 | object.name 버그픽스 (attachment 메시지) |

---

## 9. 결론

현재 Hermes Nextcloud Talk plugin은 **핵심 메시지 송수신 흐름**을 안정적으로 구현하고 있음. HMAC 서명 검증, deduplication, rate limiting, permission policy 등 프로덕션 수준의 보안 기능이 포함되어 있음.

**추가 개발 우선순위**:
1. Reaction 이벤트 처리 (Like/Undo)
2. Attachment 메시지 처리
3. Thread/Reply 완전 지원
4. Guest/Email participant 처리 강화

---

## 출처

↑ NEXTCLOUD_TALK_BOTS_API — nextcloud-talk.readthedocs.io/en/latest/bots/
↑ NEXTCLOUD_TALK_DOCS — nextcloud-talk.readthedocs.io/en/latest/
↑ HERMES_PLUGIN_CODE — /home/mg/Projects/nextcloud-talk-hermes-plugin/
↑ OPENCLAW_NC_TALK — docs.openclaw.ai/channels/nextcloud-talk
↑ GITHUB_ISSUE_6157 — github.com/zeroclaw-labs/zeroclaw/issues/6157