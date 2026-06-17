"""Nextcloud Talk webhook adapter for Hermes Agent.

Handles Nextcloud Talk bot webhooks: receives chat messages via HTTP POST,
verifies HMAC-SHA256 signatures, and sends responses back via the Talk
Bot API.

Based on the OpenClaw nextcloud-talk extension and Hermes' wecom_callback.py
pattern.

Configuration in config.yaml::

    gateway:
      platforms:
        nextcloud_talk:
          enabled: true
          extra:
            base_url: "https://your-nextcloud.example.com"
            bot_token: "your-bot-token"
            bot_secret: "your-bot-secret"
            host: "0.0.0.0"
            port: 8745
            path: "/nextcloud-talk/callback"

Or via environment variables (overrides config.yaml)::

    NEXTCLOUD_TALK_BASE_URL, NEXTCLOUD_TALK_BOT_TOKEN, NEXTCLOUD_TALK_BOT_SECRET,
    NEXTCLOUD_TALK_HOST, NEXTCLOUD_TALK_PORT, NEXTCLOUD_TALK_PATH,
    NEXTCLOUD_TALK_ALLOWED_USERS, NEXTCLOUD_TALK_GROUP_POLICY,
    NEXTCLOUD_TALK_DM_POLICY, NEXTCLOUD_TALK_ALLOWED_DM_USERS
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

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

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8745
DEFAULT_PATH = "/nextcloud-talk/callback"

# Webhook headers (case-insensitive in aiohttp)
SIGNATURE_HEADER = "x-nextcloud-talk-signature"
RANDOM_HEADER = "x-nextcloud-talk-random"

# Dedup TTL — messages older than this are considered unique again
MESSAGE_DEDUP_TTL_SECONDS = 300

# Rate limiting constants
RATE_LIMIT_WINDOW_SECONDS = 300    # 5 minutes
RATE_LIMIT_MAX_ATTEMPTS = 10       # max failures in window
RATE_LIMIT_BLOCK_SECONDS = 1800    # 30 minutes block duration


# ── Plugin helpers ────────────────────────────────────────────────────────

def check_requirements() -> bool:
    """Return True if the Nextcloud Talk adapter can be used."""
    token = os.getenv("NEXTCLOUD_TALK_BOT_TOKEN", "")
    secret = os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
    base_url = os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
    if not token or not secret or not base_url:
        logger.debug("Nextcloud Talk: required env vars not set")
        return False
    if not AIOHTTP_AVAILABLE or not HTTPX_AVAILABLE:
        logger.warning("Nextcloud Talk: aiohttp and httpx required")
        return False
    return True


def validate_config(config) -> bool:
    """Validate that the platform config has required fields."""
    extra = getattr(config, "extra", {}) or {}
    base_url = extra.get("base_url", "") or os.getenv("NEXTCLOUD_TALK_BASE_URL", "")
    bot_token = extra.get("bot_token", "") or os.getenv("NEXTCLOUD_TALK_BOT_TOKEN", "")
    bot_secret = extra.get("bot_secret", "") or os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
    return bool(base_url and bot_token and bot_secret)


def is_connected(config) -> bool:
    """Check if the platform is connected (env vars set + deps available)."""
    return check_requirements()


# ── Adapter ───────────────────────────────────────────────────────────────

class NextcloudTalkAdapter(BasePlatformAdapter):
    """Nextcloud Talk webhook adapter.

    Receives messages via HTTP POST webhook (ActivityPub-style payloads),
    verifies HMAC-SHA256 signatures, and sends responses via the Nextcloud
    Talk Bot API.
    """

    supports_code_blocks: bool = True

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
        self._bot_token = str(
            extra.get("bot_token", "") or os.getenv("NEXTCLOUD_TALK_BOT_TOKEN", "")
        )
        self._bot_secret = str(
            extra.get("bot_secret", "") or os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
        )

        # ── Rate limiting ───────────────────────────────────────────────
        # Maps IP -> list of failure timestamps (monotonic)
        self._failed_attempts: Dict[str, List[float]] = {}

        # ── Room-based permission policies ──────────────────────────────
        allowed_users_raw = (
            extra.get("allowed_users")
            or os.getenv("NEXTCLOUD_TALK_ALLOWED_USERS", "")
        )
        if isinstance(allowed_users_raw, str):
            self._allowed_users: set = set(
                u.strip() for u in allowed_users_raw.split(",") if u.strip()
            )
        elif isinstance(allowed_users_raw, (list, set)):
            self._allowed_users: set = set(str(u) for u in allowed_users_raw)
        else:
            self._allowed_users: set = set()

        self._group_policy: str = str(
            extra.get("group_policy")
            or os.getenv("NEXTCLOUD_TALK_GROUP_POLICY", "all")
        )

        self._dm_policy: str = str(
            extra.get("dm_policy")
            or os.getenv("NEXTCLOUD_TALK_DM_POLICY", "all")
        )

        allowed_dm_raw = (
            extra.get("allowed_dm_users")
            or os.getenv("NEXTCLOUD_TALK_ALLOWED_DM_USERS", "")
        )
        if isinstance(allowed_dm_raw, str):
            self._allowed_dm_users: set = set(
                u.strip() for u in allowed_dm_raw.split(",") if u.strip()
            )
        elif isinstance(allowed_dm_raw, (list, set)):
            self._allowed_dm_users: set = set(str(u) for u in allowed_dm_raw)
        else:
            self._allowed_dm_users: set = set()

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._app: Optional[web.Application] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._seen_messages: Dict[str, float] = {}
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Start the webhook HTTP server."""
        if not AIOHTTP_AVAILABLE or not HTTPX_AVAILABLE:
            logger.error("nextcloud_talk requires aiohttp and httpx")
            return False

        if not self._base_url or not self._bot_token or not self._bot_secret:
            logger.error(
                "nextcloud_talk requires base_url, bot_token, and bot_secret"
            )
            return False

        # ── Config validation (Improvement 3) ───────────────────────────
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
                "Authorization": f"Bearer {self._bot_token}",
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
        """Stop the webhook HTTP server."""
        self._mark_disconnected()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # ── Message handling ──────────────────────────────────────────────────

    async def handle_message(self, event: MessageEvent) -> None:
        """Process a received message event."""
        if not self._message_handler:
            return

        session_key = self._get_session_key(event)
        self._active_sessions.setdefault(
            session_key, asyncio.Event()
        ).set()

        task = asyncio.create_task(self._process_message(event, session_key))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_message(
        self, event: MessageEvent, session_key: str
    ) -> None:
        """Process a message through the agent."""
        handler = self._message_handler
        if not handler:
            return

        try:
            await handler(event, session_key)
        except Exception:
            logger.exception("Error processing nextcloud_talk message")
        finally:
            self._active_sessions.pop(session_key, None)

    def _get_session_key(self, event: MessageEvent) -> str:
        """Build a session key from the event source."""
        source = getattr(event, "source", None)
        room_token = (
            getattr(source, "room_token", None)
            or getattr(source, "chat_id", "")
        )
        sender = getattr(source, "user_id", "")
        return f"nc:{room_token}:{sender}"

    # ── Webhook handler ───────────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming Nextcloud Talk webhook POST."""
        try:
            body = await request.text()
            headers = request.headers

            signature = headers.get(SIGNATURE_HEADER, "")
            random_val = headers.get(RANDOM_HEADER, "")

            if not signature or not random_val:
                logger.warning(
                    "Missing signature or random header from Nextcloud"
                )
                return _json_response(
                    400, {"error": "Missing signature/random headers"}
                )

            if not self._verify_signature(
                random_val, body, self._bot_secret, signature
            ):
                # ── Rate limiting on signature failure (Improvement 1) ────
                client_ip = request.remote  # aiohttp: remote is already a string (IP)
                self._record_failed_attempt(client_ip)
                if self._is_rate_limited(client_ip):
                    logger.warning("Rate limited: %s", client_ip)
                    return _json_response(429, {"error": "Too many failed attempts"})
                # Reset failed attempts on successful auth (unblock)
                self._clear_failed_attempts(client_ip)

                logger.warning("Invalid signature from Nextcloud Talk")
                return _json_response(
                    403, {"error": "Invalid signature"}
                )

            # Successful auth — clear rate-limit tracking for this IP
            client_ip = request.remote
            self._clear_failed_attempts(client_ip)

            data = json.loads(body)
            msg_event = self._parse_message(data)
            if not msg_event:
                return _json_response(200, {"status": "ok"})

            # ── Permission check (Improvement 2) ──────────────────────
            if not self._check_permissions(msg_event):
                return _json_response(200, {"status": "permission_denied"})

            # Dedup
            msg_id = msg_event.message_id
            if msg_id and self._is_duplicate(msg_id):
                logger.debug("Skipping duplicate message %s", msg_id)
                return _json_response(200, {"status": "duplicate"})

            # Queue for gateway processing
            await self.handle_message(msg_event)
            return _json_response(200, {"status": "accepted"})

        except json.JSONDecodeError:
            logger.exception("Failed to parse Nextcloud webhook body")
            return _json_response(400, {"error": "Bad JSON"})
        except Exception:
            logger.exception("Error handling Nextcloud webhook")
            return _json_response(500, {"error": "Internal error"})

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

        # Bulk-evict expired entries
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
          "type": "Create",                  # Create | Update | Delete
          "actor": {"type": "Person", "id": "...", "name": "..."},
          "object": {
            "type": "Note", "id": "...",
            "name": "...", "content": "...",
            "mediaType": "..."
          },
          "target": {"type": "Collection", "id": "...", "name": "..."}
        }
        """
        activity_type = data.get("type", "")

        # Handle Update and Delete gracefully
        if activity_type == "Delete":
            logger.debug("Ignoring Delete activity")
            return None

        if activity_type == "Update":
            logger.debug("Ignoring Update activity")
            return None

        # Only process "Create" activities (new messages)
        if activity_type != "Create":
            logger.debug(
                "Ignoring non-Create activity type: %s", activity_type
            )
            return None

        obj = data.get("object", {})
        message_text = (obj.get("content", "") or "").strip()
        if not message_text:
            logger.debug("Empty message content, skipping")
            return None

        actor = data.get("actor", {})
        sender_id = str(actor.get("id", ""))
        sender_name = str(actor.get("name", ""))

        target = data.get("target", {})
        room_token = str(target.get("name", ""))

        message_id = str(obj.get("id", ""))
        is_group = data.get("isGroupChat", False)

        # Build SessionSource for gateway routing
        from gateway.session import SessionSource

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id=room_token,
            chat_name=room_token,
            chat_type="group" if is_group else "dm",
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
            return SendResult(
                success=False, error="No room_token in chat_id"
            )

        url = (
            f"/ocs/v2.php/apps/spreed/api/v1/bot/{room_token}/message"
        )

        payload: Dict[str, Any] = {"message": content}
        if reply_to:
            payload["replyTo"] = reply_to

        # Serialize payload for both sending and signing
        payload_body = json.dumps(payload)

        # Generate HMAC signature over random + JSON body (matching Nextcloud Bot API spec)
        random_value = secrets.token_hex(16)
        signing_input = random_value + payload_body
        signature = hmac.new(
            self._bot_secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            RANDOM_HEADER: random_value,
            SIGNATURE_HEADER: signature,
        }

        try:
            resp = await self._http_client.post(
                url,
                content=payload_body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **headers,
                },
            )
            if resp.status_code in (200, 201):
                logger.debug(
                    "Sent message to room %s (reply_to=%s)",
                    room_token, reply_to,
                )
                return SendResult(success=True)
            else:
                logger.error(
                    "Nextcloud Talk send failed: %d %s",
                    resp.status_code, resp.text[:200],
                )
                return SendResult(
                    success=False, error=f"HTTP {resp.status_code}"
                )
        except Exception as exc:
            logger.error("Nextcloud Talk send error: %s", exc)
            return SendResult(success=False, error=str(exc))

    # ── Optional overrides ────────────────────────────────────────────────

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Nextcloud Talk has no typing indicator."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return chat info."""
        return {"name": chat_id, "type": "group"}

    def format_message(self, content: str) -> str:
        """Nextcloud Talk supports rich text — pass through."""
        return content

    # ── Health check ──────────────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint — OpenClaw-compatible /healthz."""
        return _json_response(200, {"status": "healthy"})

    # ── Improvement 1: Rate Limiting ──────────────────────────────────────

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
        attempts = self._failed_attempts.get(ip, [])

        if not attempts:
            return False

        # Check if currently blocked (has an old entry that triggers block)
        recent = [t for t in attempts if now - t <= RATE_LIMIT_WINDOW_SECONDS]
        self._failed_attempts[ip] = recent

        if len(recent) >= RATE_LIMIT_MAX_ATTEMPTS:
            # Block for RATE_LIMIT_BLOCK_SECONDS
            oldest = min(recent)
            if now - oldest < RATE_LIMIT_BLOCK_SECONDS:
                return True
            # Block expired — clear and reset
            self._clear_failed_attempts(ip)
            return False

        return False

    def _clear_failed_attempts(self, ip: str) -> None:
        """Clear all failed attempt records for an IP (on successful auth)."""
        self._failed_attempts.pop(ip, None)

    # ── Improvement 2: Permission Policies ────────────────────────────────

    def _check_permissions(self, event: MessageEvent) -> bool:
        """Check if the sender is authorized to send messages.

        Returns True if allowed, False if denied (logged but not blocked).
        """
        source = getattr(event, "source", None)
        if not source:
            return True

        sender_id = getattr(source, "user_id", "")
        chat_type = getattr(source, "chat_type", "unknown")

        # Group policy check
        if chat_type == "group" and self._group_policy == "members":
            if self._allowed_users and sender_id not in self._allowed_users:
                logger.info(
                    "Permission denied: user %s not in allowed_users for group chat",
                    sender_id,
                )
                return False

        # DM policy check
        if chat_type == "dm" and self._dm_policy == "restricted":
            if self._allowed_dm_users and sender_id not in self._allowed_dm_users:
                logger.info(
                    "Permission denied: user %s not in allowed_dm_users for DM",
                    sender_id,
                )
                return False

        return True

    # ── Improvement 3: Config Validation ──────────────────────────────────

    @staticmethod
    def _validate_port(port: int) -> bool:
        """Validate that port is in the valid range 1-65535."""
        return isinstance(port, int) and 1 <= port <= 65535

    @staticmethod
    def _validate_base_url(url: str) -> bool:
        """Validate that the base URL starts with http:// or https://."""
        return isinstance(url, str) and (
            url.startswith("http://") or url.startswith("https://")
        )


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
    bot_token = (
        pconfig.extra.get("bot_token", "")
        or os.getenv("NEXTCLOUD_TALK_BOT_TOKEN", "")
    )
    bot_secret = (
        pconfig.extra.get("bot_secret", "")
        or os.getenv("NEXTCLOUD_TALK_BOT_SECRET", "")
    )
    room_token = chat_id

    if not base_url or not bot_token or not room_token:
        return {"error": "Missing NEXTCLOUD_TALK_BASE_URL, BOT_TOKEN, or chat_id"}

    url = (
        f"{base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/bot/"
        f"{room_token}/message"
    )

    # Build payload and serialize for both sending and signing
    payload_body = json.dumps({"message": message})

    # Generate HMAC signature over random + JSON body
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
                    "Authorization": f"Bearer {bot_token}",
                    "OCS-ApiRequest": "true",
                    "Content-Type": "application/json",
                    RANDOM_HEADER: random_value,
                    SIGNATURE_HEADER: signature,
                },
            )
            if resp.status_code in (200, 201):
                return {
                    "success": True,
                    "message_id": str(int(time.time() * 1000)),
                }
            return {
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}"
            }
    except Exception as exc:
        return {"error": str(exc)}


# ── Plugin registration ──────────────────────────────────────────────────

def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="nextcloud_talk",
        label="Nextcloud Talk",
        adapter_factory=lambda cfg: NextcloudTalkAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "NEXTCLOUD_TALK_BASE_URL",
            "NEXTCLOUD_TALK_BOT_TOKEN",
            "NEXTCLOUD_TALK_BOT_SECRET",
        ],
        install_hint="pip install aiohttp httpx",
        # Cron home-channel delivery via Nextcloud Talk REST API.
        standalone_sender_fn=_standalone_send,
        emoji="💬",
        allow_update_command=True,
    )