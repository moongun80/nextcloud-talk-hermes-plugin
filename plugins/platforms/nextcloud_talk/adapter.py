"""Nextcloud Talk webhook adapter for Hermes Agent.

Receives chat messages via HTTP POST webhook (ActivityPub-style payloads),
verifies HMAC-SHA256 signatures, and sends responses back via the Talk
Bot API.

Authentication is HMAC-SHA256 signature-based only -- no bot token concept.
Receiving (NC -> bot): server signs with secret -> bot verifies.
Sending (bot -> NC): bot signs with secret -> server verifies.

Thin protocol bridge -- session management, message routing, and background
task lifecycle are handled by BasePlatformAdapter.

Configuration in config.yaml::

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

Or via environment variables (overrides config.yaml)::

    NEXTCLOUD_TALK_BASE_URL, NEXTCLOUD_TALK_BOT_SECRET,
    NEXTCLOUD_TALK_HOST, NEXTCLOUD_TALK_PORT, NEXTCLOUD_TALK_PATH,
    NEXTCLOUD_TALK_ALLOWED_USERS, NEXTCLOUD_TALK_GROUP_POLICY,
    NEXTCLOUD_TALK_DM_POLICY, NEXTCLOUD_TALK_ALLOWED_DM_USERS,
    NEXTCLOUD_TALK_TRUSTED_PROXIES, NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH,
    NEXTCLOUD_TALK_HOME_CHANNEL
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Set

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8745
DEFAULT_PATH = "/nextcloud-talk/callback"
DEFAULT_MAX_MESSAGE_LENGTH = 32000  # Nextcloud Bot API limit (newer servers)

# ── Receive-direction headers (NC -> bot) ────────────────────────────────
SIGNATURE_HEADER = "x-nextcloud-talk-signature"
RANDOM_HEADER = "x-nextcloud-talk-random"

# ── Send-direction headers (bot -> NC) ───────────────────────────────────
# These MUST include the "-Bot-" segment per the Nextcloud Talk Bot API spec.
# Using receive-direction headers causes 401 Unauthenticated.
SEND_RANDOM_HEADER = "x-nextcloud-talk-bot-random"
SEND_SIGNATURE_HEADER = "x-nextcloud-talk-bot-signature"

# ── Dedup TTL -- messages older than this are considered unique again ────
MESSAGE_DEDUP_TTL_SECONDS = 300

# ── Replay protection TTL (separate from message dedup) ──────────────────
REPLAY_RANDOM_TTL_SECONDS = 300

# ── Rate limiting constants ─────────────────────────────────────────────
RATE_LIMIT_WINDOW_SECONDS = 300    # 5 minutes
RATE_LIMIT_MAX_ATTEMPTS = 10       # max failures in window
RATE_LIMIT_BLOCK_SECONDS = 1800    # 30 minutes block duration

# ── Trusted proxies (CIDR notation) ──────────────────────────────────────
# Empty set = trust no proxies (use X-Forwarded-For directly).
# Set to {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"} for private networks.
_DEFAULT_TRUSTED_PROXIES: Set[str] = set()


# ── Plugin helpers ────────────────────────────────────────────────────────

def check_requirements() -> bool:
    """Return True if the Nextcloud Talk adapter can be used.

    Checks both environment variables and module-level config state
    (populated by __init__ when the adapter is instantiated).
    """
    global _DEFAULT_TRUSTED_PROXIES

    # Module-level state set by __init__ (covers config.yaml-only setup)
    if getattr(_check_requirements_state, "_has_config", False):
        if not getattr(_check_requirements_state, "_has_secret", False):
            logger.debug("Nextcloud Talk: bot_secret not in config")
            return False
        if not getattr(_check_requirements_state, "_has_base_url", False):
            logger.debug("Nextcloud Talk: base_url not in config")
            return False
    else:
        # Fallback: check env vars
        secret = os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
        base_url = os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
        if not secret or not base_url:
            logger.debug("Nextcloud Talk: required env vars not set")
            return False

    if not AIOHTTP_AVAILABLE or not HTTPX_AVAILABLE:
        logger.warning("Nextcloud Talk: aiohttp and httpx required")
        return False

    # Parse trusted proxies from env if set
    trusted_str = os.getenv("NEXTCLOUD_TALK_TRUSTED_PROXIES", "")
    if trusted_str:
        _DEFAULT_TRUSTED_PROXIES = {
            p.strip() for p in trusted_str.split(",") if p.strip()
        }

    return True


# Module-level state container for check_requirements (bridge config.yaml gap)
class _check_requirements_state:
    _has_config = False
    _has_secret = False
    _has_base_url = False

    @classmethod
    def reset(cls):
        """Reset state for test isolation."""
        cls._has_config = False
        cls._has_secret = False
        cls._has_base_url = False


def validate_config(config) -> bool:
    """Validate that the platform config has required fields."""
    extra = getattr(config, "extra", {}) or {}
    base_url = extra.get("base_url", "") or os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
    bot_secret = extra.get("bot_secret", "") or os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
    return bool(base_url and bot_secret)


# ── Adapter ───────────────────────────────────────────────────────────────

class NextcloudTalkAdapter(BasePlatformAdapter):
    """Nextcloud Talk webhook adapter.

    Thin protocol bridge: receives webhooks, verifies signatures, parses
    ActivityPub payloads into MessageEvents, and delegates to the gateway
    for session management and routing.
    """

    supports_code_blocks: bool = True
    _max_message_length: int = int(os.getenv("NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH", "65536"))

    def __init__(self, config, **kwargs):
        platform = Platform("nextcloud_talk")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self._host = str(
            extra.get("host") or os.getenv("NEXTCLOUD_TALK_HOST", DEFAULT_HOST)
        )
        self._port = int(
            extra.get("port") or os.getenv("NEXTCLOUD_TALK_PORT", DEFAULT_PORT)
        )
        self._path = str(
            extra.get("path") or os.getenv("NEXTCLOUD_TALK_PATH", DEFAULT_PATH)
        )
        self._base_url = str(
            extra.get("base_url", "") or os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
        )
        self._bot_secret = str(
            extra.get("bot_secret", "") or os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
        )

        # Max message length (prevent 413 errors from oversized payloads)
        self._max_message_length: int = int(
            extra.get("max_message_length")
            or os.getenv("NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH", DEFAULT_MAX_MESSAGE_LENGTH)
        )

        # ── Rate limiting ───────────────────────────────────────────────
        self._failed_attempts: Dict[str, List[float]] = {}
        self._blocked_ips: Dict[str, float] = {}

        # ── Replay protection ───────────────────────────────────────────
        # Track seen random values with monotonic timestamps.
        # Prevents replay attacks even after message dedup TTL expires.
        self._seen_randoms: Dict[str, float] = {}

        # ── Message deduplication ───────────────────────────────────────
        # Track seen message IDs with monotonic timestamps.
        self._seen_messages: Dict[str, float] = {}

        # ── Room-based permission policies ──────────────────────────────
        # Use `is None` checks (not `or`) so empty lists are preserved as
        # valid values (empty whitelist = deny all).
        allowed_users_raw = extra.get("allowed_users")
        if allowed_users_raw is None:
            allowed_users_raw = os.getenv("NEXTCLOUD_TALK_ALLOWED_USERS", "")
        if isinstance(allowed_users_raw, str):
            self._allowed_users = set(
                u.strip() for u in allowed_users_raw.split(",") if u.strip()
            )
        elif isinstance(allowed_users_raw, (list, set)):
            self._allowed_users = set(str(u) for u in allowed_users_raw)
        else:
            self._allowed_users = set()

        self._group_policy: str = str(
            extra.get("group_policy")
            or os.getenv("NEXTCLOUD_TALK_GROUP_POLICY", "all")
        )

        self._dm_policy: str = str(
            extra.get("dm_policy")
            or os.getenv("NEXTCLOUD_TALK_DM_POLICY", "all")
        )

        allowed_dm_raw = extra.get("allowed_dm_users")
        if allowed_dm_raw is None:
            allowed_dm_raw = os.getenv("NEXTCLOUD_TALK_ALLOWED_DM_USERS", "")
        if isinstance(allowed_dm_raw, str):
            self._allowed_dm_users = set(
                u.strip() for u in allowed_dm_raw.split(",") if u.strip()
            )
        elif isinstance(allowed_dm_raw, (list, set)):
            self._allowed_dm_users = set(str(u) for u in allowed_dm_raw)
        else:
            self._allowed_dm_users = set()

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._app: Optional[web.Application] = None
        self._http_client: Optional[httpx.AsyncClient] = None

        # ── Register module-level state for check_requirements ──────────
        _check_requirements_state._has_config = True
        _check_requirements_state._has_secret = bool(self._bot_secret)
        _check_requirements_state._has_base_url = bool(self._base_url)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Start the webhook HTTP server."""
        if not AIOHTTP_AVAILABLE or not HTTPX_AVAILABLE:
            logger.error("nextcloud_talk requires aiohttp and httpx")
            return False

        if not self._base_url or not self._bot_secret:
            logger.error(
                "nextcloud_talk requires base_url and bot_secret",
            )
            return False

        if not self._validate_port(self._port):
            logger.error("Invalid port: %d (must be 1-65535)", self._port)
            return False

        if not self._validate_base_url(self._base_url):
            logger.error(
                "Invalid base_url: %s (must start with http:// or https://)",
                self._base_url,
            )
            return False

        self._app = web.Application()
        self._app.router.add_post(self._path, self._handle_webhook)
        self._app.router.add_get("/healthz", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        self._http_client = httpx.AsyncClient(
            base_url=self._base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0),
            headers={
                "OCS-ApiRequest": "true",
            },
        )

        self._running = True
        self._mark_connected()
        logger.info(
            "Nextcloud Talk adapter listening on http://%s:%d%s",
            self._host, self._port, self._path,
        )
        return True

    async def disconnect(self) -> None:
        """Stop the webhook HTTP server and clean up all resources."""
        self._running = False
        self._mark_disconnected()

        # Clean up all resources in reverse order of creation
        if self._site:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None

        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

        if self._app:
            try:
                await self._app.shutdown()
            except Exception:
                pass
            self._app = None

        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

    # ── Webhook handler ───────────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming Nextcloud Talk webhook POST."""
        try:
            body = await request.text()
            headers = request.headers

            signature = headers.get(SIGNATURE_HEADER, "")
            random_val = headers.get(RANDOM_HEADER, "")

            if not signature or not random_val:
                logger.warning("Missing signature or random header from Nextcloud")
                return _json_response(400, {"error": "Missing signature/random headers"})

            # Get real client IP (respecting X-Forwarded-For for proxied setups)
            client_ip = self._get_client_ip(request)

            # ── Replay protection: reject reused random values ────────────
            if self._is_replayed_random(random_val):
                logger.warning("Rejected replayed random value from %s", client_ip)
                return _json_response(400, {"error": "Replayed request"})

            if not self._verify_signature(random_val, body, self._bot_secret, signature):
                self._record_failed_attempt(client_ip)
                if self._is_rate_limited(client_ip):
                    logger.warning("Rate limited: %s", client_ip)
                    return _json_response(429, {"error": "Too many failed attempts"})
                logger.warning("Invalid signature from Nextcloud Talk")
                return _json_response(403, {"error": "Invalid signature"})

            self._clear_failed_attempts(client_ip)

            # ── Parse payload ───────────────────────────────────────────
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                logger.error("Received non-JSON webhook body (expected ActivityPub JSON)")
                return _json_response(400, {"error": "Body must be valid JSON"})

            msg_event = self._parse_message(data)
            if not msg_event:
                return _json_response(200, {"status": "ok"})

            if not self._check_permissions(msg_event):
                return _json_response(200, {"status": "permission_denied"})

            msg_id = msg_event.message_id
            if msg_id and self._is_duplicate(msg_id):
                logger.debug("Skipping duplicate message %s", msg_id)
                return _json_response(200, {"status": "duplicate"})

            # Delegate to base class for gateway routing
            await self.handle_message(msg_event)
            return _json_response(200, {"status": "accepted"})

        except Exception:
            logger.exception("Error handling Nextcloud webhook")
            return _json_response(500, {"error": "Internal error"})

    # ── Client IP extraction (X-Forwarded-For) ────────────────────────────

    def _get_client_ip(self, request: web.Request) -> str:
        """Get the real client IP, respecting X-Forwarded-For for proxied setups.

        If trusted proxies are configured, uses the last untrusted IP in
        X-Forwarded-For. Otherwise falls back to request.remote.
        """
        trusted = _DEFAULT_TRUSTED_PROXIES

        xff = request.headers.get("X-Forwarded-For")
        if xff and trusted:
            # X-Forwarded-For can contain multiple IPs: client, proxy1, proxy2
            ips = [ip.strip() for ip in xff.split(",")]
            # Walk from right to left, return the last untrusted IP
            for ip in reversed(ips):
                if not self._is_trusted_ip(ip, trusted):
                    return ip
            # All IPs are trusted, use the leftmost (original client)
            return ips[0] if ips else request.remote or "unknown"
        # No trusted proxies configured: ignore XFF to prevent spoofing.
        # When no trusted proxies are set, XFF is untrustworthy -- any client
        # can forge it to bypass rate limits and IP blocks.

        return request.remote or "0.0.0.0"

    @staticmethod
    def _is_trusted_ip(ip: str, trusted_cidrs: Set[str]) -> bool:
        """Check if IP is in a trusted CIDR range (simple prefix matching)."""
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip)
            for cidr in trusted_cidrs:
                try:
                    if addr in ipaddress.ip_network(cidr, strict=False):
                        return True
                except ValueError:
                    continue
        except ValueError:
            pass
        return False

    # ── Replay protection ─────────────────────────────────────────────────

    def _is_replayed_random(self, random_val: str) -> bool:
        """Check if this random value was already seen (replay attack prevention).

        Evicts stale entries on each call for O(1) amortized cost.
        """
        now = time.monotonic()

        # Evict stale entries
        expired = [
            k for k, ts in self._seen_randoms.items()
            if now - ts > REPLAY_RANDOM_TTL_SECONDS
        ]
        for k in expired:
            del self._seen_randoms[k]

        if random_val in self._seen_randoms:
            return True

        self._seen_randoms[random_val] = now
        return False

    # ── Signature verification ────────────────────────────────────────────

    @staticmethod
    def _verify_signature(
        random_val: str, body: str, secret: str, signature: str
    ) -> bool:
        """Verify HMAC-SHA256 signature.

        Signature = HMAC-SHA256(random + body, secret)
        Uses constant-time comparison to prevent timing attacks.
        """
        computed = hmac.new(
            secret.encode("utf-8"),
            (random_val + body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed.lower(), signature.lower())

    # ── Deduplication ─────────────────────────────────────────────────────

    def _is_duplicate(self, message_id: str) -> bool:
        """Check if message_id was recently seen.

        Evicts stale entries on each call for O(1) amortized cost.
        """
        if not message_id:
            return False

        now = time.monotonic()

        expired = [
            k for k, ts in self._seen_messages.items()
            if now - ts > MESSAGE_DEDUP_TTL_SECONDS
        ]
        for k in expired:
            del self._seen_messages[k]

        if message_id in self._seen_messages:
            return True

        self._seen_messages[message_id] = now
        return False

    # ── Payload parsing ───────────────────────────────────────────────────

    def _parse_message(self, data: Dict[str, Any]) -> Optional[MessageEvent]:
        """Parse Nextcloud Talk ActivityPub-style webhook into a MessageEvent.

        Expected structure:
        {
          "type": "Create",
          "actor": {"type": "Person", "id": "...", "name": "..."},
          "object": {
            "type": "Note", "id": "...",
            "name": "...", "content": "...",  <-- JSON-encoded dict per spec
            "mediaType": "..."
          },
          "target": {"type": "Collection", "id": "<room_token>", "name": "<room_name>"}
        }
        """
        activity_type = data.get("type", "")

        if activity_type in ("Delete", "Update"):
            logger.debug("Ignoring %s activity", activity_type)
            return None

        if activity_type != "Create":
            logger.debug("Ignoring non-Create activity type: %s", activity_type)
            return None

        obj = data.get("object", {})

        # ── C1 FIX: Parse object.content as JSON (per spec) ─────────────
        # Per Nextcloud Talk Bot API spec, object.content is a JSON-encoded
        # dictionary: {"message": "...", "parameters": {...}}
        # Fall back to plain text if JSON parsing fails.
        raw_content = obj.get("content", "") or ""
        message_text = self._parse_content(raw_content)
        if not message_text:
            logger.debug("Empty message content, skipping")
            return None

        actor = data.get("actor", {})
        sender_id = str(actor.get("id", ""))
        sender_name = str(actor.get("name", "") or "")

        target = data.get("target", {})
        # ── M2 FIX: target.id is the room token directly, not a URL ────
        # Per spec: "target.id -- The token of the conversation"
        room_token = str(target.get("id", "") or "").strip()
        if not room_token:
            logger.debug("No room token in target.id, skipping")
            return None

        # Legacy fallback: check isGroupChat, then default to DM.
        is_group = data.get("isGroupChat", False)
        chat_type = "group" if is_group else "dm"

        room_name = str(target.get("name", "") or "")

        message_id = str(obj.get("id", ""))

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id=room_token,
            chat_name=room_name or room_token,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_name,
            thread_id=None,
        )

        return MessageEvent(
            text=message_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=message_id,
        )

    @staticmethod
    def _parse_content(raw_content: str) -> str:
        """Parse object.content field.

        Per spec, content is JSON-encoded: {"message": "...", "parameters": {...}}
        Falls back to raw text if not valid JSON.
        """
        if not raw_content:
            return ""

        # Try JSON parsing first (per spec)
        try:
            content_obj = json.loads(raw_content)
            if isinstance(content_obj, dict):
                message = content_obj.get("message", "") or ""
                params = content_obj.get("parameters", {})
                if isinstance(params, dict) and message:
                    # Render placeholders with actual display names
                    for key, val in params.items():
                        if isinstance(val, dict):
                            display_name = val.get("displayName", "") or val.get("name", key)
                            message = message.replace("{" + key + "}", str(display_name))
                    return message.strip()
                return message.strip()
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # Fallback: treat as plain text
        return raw_content.strip()

    # ── Outbound: send response ───────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to Nextcloud Talk via the Bot API."""
        if not self._http_client or not self._running:
            return SendResult(success=False, error="Adapter not connected")

        room_token = chat_id
        if not room_token:
            return SendResult(success=False, error="No room_token in chat_id")

        # ── m5 FIX: Sanitize room_token for URL path ────────────────────
        # Only allow safe token characters (alphanumeric, hyphen, underscore)
        if not re.match(r'^[a-zA-Z0-9\-_]+$', room_token):
            logger.warning("Unsafe room_token: %r, rejecting", room_token)
            return SendResult(success=False, error="Invalid room_token")

        url = f"/ocs/v2.php/apps/spreed/api/v1/bot/{room_token}/message"

        # ── m9 FIX: Enforce max message length ──────────────────────────
        if len(content) > self._max_message_length:
            logger.warning(
                "Message truncated from %d to %d chars",
                len(content), self._max_message_length,
            )
            content = content[:self._max_message_length]

        payload: Dict[str, Any] = {"message": content}
        # ── C3 FIX: Convert replyTo to int (spec requires integer) ──────
        if reply_to:
            try:
                payload["replyTo"] = int(reply_to)
            except (ValueError, TypeError):
                logger.warning("Invalid reply_to value: %r, sending without reply", reply_to)

        payload_body = json.dumps(payload)

        random_value = secrets.token_hex(16)
        signing_input = random_value + payload_body
        signature = hmac.new(
            self._bot_secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            resp = await self._http_client.post(
                url,
                content=payload_body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    # ── C2 FIX: Use SEND-specific headers (-Bot- variant) ─
                    SEND_RANDOM_HEADER: random_value,
                    SEND_SIGNATURE_HEADER: signature,
                },
            )
            if resp.status_code in (200, 201):
                # ── M8 FIX: Extract message_id from server response ──────
                message_id = None
                try:
                    resp_data = resp.json()
                    if isinstance(resp_data, dict):
                        # Response wrapper: {"ocs":{"data":{"message":12345}}}
                        ocs = resp_data.get("ocs", {})
                        if isinstance(ocs, dict):
                            data = ocs.get("data", {})
                        else:
                            data = ocs
                        if isinstance(data, dict):
                            message_id = str(data.get("message", ""))
                        elif isinstance(data, int):
                            message_id = str(data)
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

                logger.debug(
                    "Sent message to room %s (reply_to=%s, server_msg_id=%s)",
                    room_token, reply_to, message_id,
                )
                return SendResult(success=True, message_id=message_id)

            logger.error("Nextcloud Talk send failed: %d %s", resp.status_code, resp.text[:200])
            return SendResult(success=False, error=f"HTTP {resp.status_code}")
        except Exception as exc:
            logger.error("Nextcloud Talk send error: %s", exc)
            return SendResult(success=False, error=str(exc))

    # ── Optional overrides ────────────────────────────────────────────────

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Nextcloud Talk has no typing indicator."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return chat info.

        Without a live session lookup, we can only return basic info.
        """
        return {"name": chat_id, "type": "unknown"}

    def format_message(self, content: str) -> str:
        """Nextcloud Talk supports rich text -- pass through."""
        return content

    # ── Health check ──────────────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint -- OpenClaw-compatible /healthz."""
        return _json_response(200, {"status": "healthy"})

    # ── Rate limiting ─────────────────────────────────────────────────────

    def _record_failed_attempt(self, ip: str) -> None:
        """Record a failed authentication attempt for the given IP."""
        now = time.monotonic()
        self._failed_attempts.setdefault(ip, []).append(now)

    def _is_rate_limited(self, ip: str) -> bool:
        """Check if the IP is rate-limited.

        Returns True if the IP has exceeded RATE_LIMIT_MAX_ATTEMPTS failures
        within RATE_LIMIT_WINDOW_SECONDS. If blocked, returns True and
        records the block duration.
        """
        now = time.monotonic()

        # Check explicit block first (survives window expiry)
        block_expiry = self._blocked_ips.get(ip)
        if block_expiry is not None:
            if now < block_expiry:
                return True
            del self._blocked_ips[ip]
            self._failed_attempts.pop(ip, None)
            return False

        attempts = self._failed_attempts.get(ip, [])
        if not attempts:
            return False

        # Prune entries outside the window
        recent = [t for t in attempts if now - t <= RATE_LIMIT_WINDOW_SECONDS]
        self._failed_attempts[ip] = recent

        if len(recent) >= RATE_LIMIT_MAX_ATTEMPTS:
            self._blocked_ips[ip] = now + RATE_LIMIT_BLOCK_SECONDS
            return True

        return False

    def _clear_failed_attempts(self, ip: str) -> None:
        """Clear all failed attempt records for an IP (on successful auth)."""
        self._failed_attempts.pop(ip, None)
        self._blocked_ips.pop(ip, None)

    # ── Permission policies ───────────────────────────────────────────────

    def _check_permissions(self, event: MessageEvent) -> bool:
        """Check if the sender is authorized to send messages.

        Returns True if allowed, False if denied (logged but not blocked).
        """
        source = getattr(event, "source", None)
        if not source:
            return True

        sender_id = getattr(source, "user_id", "")
        chat_type = getattr(source, "chat_type", "unknown")

        if chat_type == "group" and self._group_policy == "members":
            if sender_id not in self._allowed_users:
                logger.info("Permission denied: user %s not in allowed_users for group chat", sender_id)
                return False

        if chat_type == "dm" and self._dm_policy == "restricted":
            if sender_id not in self._allowed_dm_users:
                logger.info("Permission denied: user %s not in allowed_dm_users for DM", sender_id)
                return False

        return True

    # ── Config validation ─────────────────────────────────────────────────

    @staticmethod
    def _validate_port(port: int) -> bool:
        """Validate that port is in the valid range 1-65535."""
        return isinstance(port, int) and 1 <= port <= 65535

    @staticmethod
    def _validate_base_url(url: str) -> bool:
        """Validate that the base URL starts with http:// or https://."""
        return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


# ── Helpers ───────────────────────────────────────────────────────────────

def _json_response(status: int, body: Dict[str, Any]) -> Any:
    """Return an aiohttp JSON response."""
    if web is None:
        raise RuntimeError("aiohttp not available")
    return web.Response(
        status=status,
        text=json.dumps(body),
        content_type="application/json",
    )


# ── Standalone sender (cron out-of-process) ──────────────────────────────

async def _standalone_send(
    pconfig, chat_id, message, *, thread_id=None, **kwargs
):
    """Send a message via Nextcloud Talk without a live gateway adapter.

    Opens an ephemeral HTTP connection, sends the message, and returns.
    Used by cron jobs that run in a separate process from the gateway.
    """
    import httpx as _httpx

    base_url = (
        pconfig.extra.get("base_url", "")
        or os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
    )
    bot_secret = (
        pconfig.extra.get("bot_secret", "")
        or os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
    )
    room_token = chat_id

    if not base_url or not bot_secret or not room_token:
        return {"error": "Missing NEXTCLOUD_TALK_BASE_URL, BOT_SECRET, or chat_id"}

    # Sanitize room_token for URL path
    if not re.match(r'^[a-zA-Z0-9\-_]+$', room_token):
        return {"error": f"Invalid room_token: {room_token!r}"}

    url = (
        f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/bot/"
        f"{room_token}/message"
    )

    payload_body = json.dumps({"message": message})

    random_value = secrets.token_hex(16)
    signing_input = random_value + payload_body
    signature = hmac.new(
        bot_secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                content=payload_body.encode("utf-8"),
                headers={
                    "OCS-ApiRequest": "true",
                    "Content-Type": "application/json",
                    # ── C2 FIX: Use SEND-specific headers ────────────
                    SEND_RANDOM_HEADER: random_value,
                    SEND_SIGNATURE_HEADER: signature,
                },
            )
            if resp.status_code in (200, 201):
                # ── m4 FIX: Extract real message_id from response ────
                message_id = None
                try:
                    resp_data = resp.json()
                    if isinstance(resp_data, dict):
                        ocs = resp_data.get("ocs", {})
                        if isinstance(ocs, dict):
                            data = ocs.get("data", {})
                        else:
                            data = ocs
                        if isinstance(data, dict):
                            message_id = str(data.get("message", ""))
                        elif isinstance(data, int):
                            message_id = str(data)
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
                return {"success": True, "message_id": message_id}
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}


# ── Plugin registration ──────────────────────────────────────────────────

def register(ctx) -> None:
    """Plugin entry point -- called by the Hermes plugin system."""

    def _env_enablement() -> dict | None:
        base_url = os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
        bot_secret = os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
        if base_url and bot_secret:
            return {"extra": {"base_url": base_url, "bot_secret": bot_secret}}
        return None

    ctx.register_platform(
        name="nextcloud_talk",
        label="Nextcloud Talk",
        adapter_factory=lambda cfg: NextcloudTalkAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[
            "NEXTCLOUD_TALK_BASE_URL",
            "NEXTCLOUD_TALK_BOT_SECRET",
        ],
        install_hint="pip install aiohttp httpx",
        standalone_sender_fn=_standalone_send,
        emoji="💬",
        allow_update_command=True,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="NEXTCLOUD_TALK_HOME_CHANNEL",
        platform_hint="You are communicating via Nextcloud Talk. Use plain text or simple formatting.",
        max_message_length=int(
            os.getenv("NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH", DEFAULT_MAX_MESSAGE_LENGTH)
        ),
    )
