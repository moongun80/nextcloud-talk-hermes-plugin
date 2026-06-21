# Nextcloud Talk Bot — 보안 감사 & 멀티봇 구조 검토

## 1. 현재 기능 정상화

### ✅ 이미 완료된 부분
- 서명 계산 버그 수정 (`payload_body` → `content`, `token_hex(16)` → `token_hex(32)`)
- hermes-agent / 프로젝트 / 글로벌 플러그인 3곳 동기화 완료
- 트리니티 게이트웨이 재시작 완료

---

## 2. 보안 취약점 분석

### 🔴 CRITICAL

#### 2.1 요청 본문 크기 제한 없음
```python
# adapter.py:356
body = await request.text()  # 무제한 메모리 할당
```
**문제**: 공격자가 GB 단위 payload를 보내면 OOM DoS 발생  
**해결**: aiohttp의 `max_field_size` / `read_limit` 설정 필요

#### 2.2 아웃바운드 요청 속도 제한 없음
```python
# adapter.py:696-706
resp = await self._http_client.post(...)  # 무제한并发
```
**문제**: 봇이 동시에 수백 개의 메시지를 보낼 수 있어 Nextcloud 서버 과부하  
**해결**: outbound rate limiter 추가 (초당 N 메시지 제한)

### 🟡 HIGH

#### 2.3 Webhook 처리 타임아웃 없음
```python
# adapter.py:404
await self.handle_message(msg_event)  # 블록되면 webhook 응답 안 돌아감
```
**문제**: AI 응답 생성 중 블로킹되면 HTTP 연결이 열린 상태로 유지  
**해결**: `asyncio.wait_for(handle_message, timeout=30)` 적용

#### 2.4 메모리 누수 가능성 (seen_randoms / seen_messages)
```python
# adapter.py:463-468
expired = [k for k, ts in self._seen_randoms.items() if now - ts > TTL]
for k in expired:
    del self._seen_randoms[k]
```
**문제**: eviction이 호출 시점에만 발생. 고트래픽 시 메모리 누적  
**해결**: LRU dict 또는 주기적 background cleanup task

#### 2.5 메시지 인코딩 검증 없음
```python
# adapter.py:688
signing_input = random_value + stripped  # UTF-8 가정
```
**문제**: 비정상 인코딩 메시지 서명 불일치 → 400 응답  
**해결**: `errors='replace'` 또는 strict mode 명시

### 🟢 MEDIUM

#### 2.6 X-Forwarded-For 기본값
```python
# adapter.py:105
_DEFAULT_TRUSTED_PROXIES: Set[str] = set()  # 빈 집합 = XFF 무시
```
**현재**: 보안상 올바른 동작 (XFF 위조 방지)  
**권장**: 문서화하여 운영자가 프록시 설정 시 명시적으로 trusted proxy 추가하도록 유도

#### 2.7 에러 응답 정보 누출
```python
# adapter.py:714
logger.error("Nextcloud Talk send failed: %d %s", resp.status_code, resp.text[:200])
```
**문제**: Nextcloud 서버 응답이 로깅됨 (디버깅용이지만 민감정보 포함 가능)  
**해결**: `resp.text[:100]`으로 제한 + sensitive field 필터링

#### 2.8 Secret 평문 저장
```python
# adapter.py:200
self._bot_secret = str(extra.get("bot_secret", ""))
```
**현재**: HMAC 서명에 불가피  
**권장**: `_bot_secret` 접근 시 `logging`에서 `[REDACTED]`로 마스킹

---

## 3. 멀티봇 구조 개선 검토

### 현재 아키텍처
```
Profile A (lucy)  → Gateway A (PID 1) → Adapter A (port 8745, room_token X)
Profile B (trinity) → Gateway B (PID 2) → Adapter B (port 8746, room_token Y)
Profile C (neo)     → Gateway C (PID 3) → Adapter C (port 8747, room_token Z)
```

**장점**: 완전 격리, 각 프로필 독립 설정  
**단점**: 포트 관리 번거로움, 설정 중복, 단일 호스트에서 여러 포트 오픈

### 개선안 A: 단일 어댑터 멀티룸 (추천)

```yaml
# config.yaml - 한 프로필에서 여러 봇 관리
gateway:
  platforms:
    nextcloud_talk:
      enabled: true
      extra:
        base_url: "https://cloud.example.com"
        bots:
          - name: "personal-bot"
            room_token: "abc123"
            bot_secret: "secret-for-room-A"
            port: 8745
          - name: "team-bot"
            room_token: "xyz789"
            bot_secret: "secret-for-room-B"
            port: 8746
```

**장점**: 
- 설정 중앙화
- 코드 중복 제거
- 공통 리소스 공유 (httpx pool 등)

**단점**:
- 한 번에 하나의 포트만 바인딩 가능 (포트당 어댑터 인스턴스 필요)
- 실제 Nextcloud Bot API는 room_token당 별도 webhook URL 등록 필요

### 개선안 B: 다중 프로필 + 포트 자동 할당 (실용적)

```python
# adapter.py - 포트 충돌 자동 감지 및 할당
def _resolve_port(self, requested_port):
    import socket
    for port in range(requested_port, requested_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('0.0.0.0', port)) != 0:
                return port
    raise RuntimeError("No available port in range")
```

**장점**: 
- 기존 아키텍처 유지
- 포트 충돌 자동 해결
- 최소 코드 변경

### 개선안 C: 단일 포트 + 경로 기반 라우팅

```
POST /nextcloud-talk/bot/abc123/callback  → room A
POST /nextcloud-talk/bot/xyz789/callback  → room B
```

**장점**: 포트 1개만 필요  
**단점**: Nextcloud Bot API가 단일 webhook URL만 지원하므로 구현 불가

### 결론: 개선안 B 권장

실제 Nextcloud Bot API는 **room_token당 별도 webhook URL**을 등록해야 하므로, 개선안 C는 불가능. 개선안 A는 복잡도가 너무 높음.

**최선책**: 기존 다중 프로필 구조 유지하되 **포트 자동 할당** + **설정 템플릿** 추가.

---

## 4. 수정 우선순위

| 순위 | 항목 | 영향도 | 난이도 |
|------|------|--------|--------|
| 1 | 요청 본문 크기 제한 | 🔴 CRITICAL | 낮음 |
| 2 | Webhook 처리 타임아웃 | 🟡 HIGH | 낮음 |
| 3 | Outbound rate limiter | 🔴 CRITICAL | 보통 |
| 4 | 메모리 누수 개선 | 🟡 HIGH | 낮음 |
| 5 | 에러 응답 정보 제한 | 🟢 MEDIUM | 낮음 |
| 6 | 포트 자동 할당 | — | 낮음 |