"""Integration tests for NextcloudTalkAdapter webhook flow.

Uses aiohttp test client to exercise the full _handle_webhook() pipeline:
signature verification -> message parsing -> permission check -> response.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Add the Hermes gateway so gateway imports resolve
HERMES_GATEWAY = os.path.expanduser("~/.hermes/hermes-agent")
if HERMES_GATEWAY not in sys.path:
    sys.path.append(HERMES_GATEWAY)

from plugins.platforms.nextcloud_talk.adapter import (
    NextcloudTalkAdapter,
    SIGNATURE_HEADER,
    RANDOM_HEADER,
)

try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False


def _sign(payload: dict, secret: str):
    """Generate random + signature for a payload.

    Signature is over random + raw body (matching _verify_signature spec).
    """
    import secrets
    random_val = secrets.token_hex(32)  # 64-char random
    body = json.dumps(payload)

    sig = hmac.new(
        secret.encode("utf-8"),
        (random_val + body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return random_val, sig, body


def _make_test_adapter(extra: dict | None = None) -> NextcloudTalkAdapter:
    """Create a minimal adapter for testing."""
    os.environ["NEXTCLOUD_TALK_BASE_URL"] = "https://nc.example.com"
    os.environ["NEXTCLOUD_TALK_BOT_SECRET"] = "test-secret"

    config_extra = extra or {
        "base_url": "https://nc.example.com",
        "bot_secret": "test-secret",
        "host": "127.0.0.1",
        "port": 9999,
        "path": "/test-callback",
    }

    config = SimpleNamespace(extra=config_extra)

    adapter = NextcloudTalkAdapter(config)
    return adapter


# ── Skip if aiohttp not available ─────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    not AIOHTTP_AVAILABLE, reason="aiohttp not installed"
)


@pytest.fixture
def adapter():
    """Return a NextcloudTalkAdapter instance (not connected)."""
    return _make_test_adapter()


def _build_app(adapter):
    """Build an aiohttp app with the adapter's webhook handler."""
    app = web.Application()
    app.router.add_post(
        adapter._path, adapter._handle_webhook
    )
    app.router.add_get("/healthz", adapter._handle_health)
    return app


# ── Integration tests ────────────────────────────────────────────────────

class TestWebhookFullFlow:
    """End-to-end tests for the webhook handler."""

    @pytest.mark.asyncio
    async def test_valid_webhook_accepted(self, adapter):
        """Valid signature + valid message -> 200 accepted."""
        payload = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User One"},
            "object": {
                "type": "Note",
                "id": "msg-100",
                "content": "Hello integration test!",
                "mediaType": "text/plain",
            },
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
            "isGroupChat": False,
        }
        random_val, sig, body = _sign(payload, "test-secret")

        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: sig,
                    RANDOM_HEADER: random_val,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected(self, adapter):
        """Invalid signature -> 403 forbidden."""
        payload = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User One"},
            "object": {"type": "Note", "id": "msg-200", "content": "test"},
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        random_val, _, body = _sign(payload, "test-secret")

        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: "bad-signature",
                    RANDOM_HEADER: random_val,
                },
            )
            assert resp.status == 403
            data = await resp.json()
            assert "Invalid signature" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_headers_rejected(self, adapter):
        """Missing signature/random headers -> 400 bad request."""
        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(adapter._path, data="{}")
            assert resp.status == 400
            data = await resp.json()
            assert "Missing" in data["error"]

    @pytest.mark.asyncio
    async def test_valid_non_create_returns_ok(self, adapter):
        """Valid signature + non-Create activity -> 200 ok."""
        sample_payload = {"type": "Update", "object": {"content": "x"}}
        random_val, valid_sig, body = _sign(sample_payload, "test-secret")
        
        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: valid_sig,
                    RANDOM_HEADER: random_val,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_duplicate_message_ignored(self, adapter):
        """Same message_id twice -> 200 duplicate."""
        payload = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User One"},
            "object": {
                "type": "Note",
                "id": "msg-dup",
                "content": "Duplicate test",
                "mediaType": "text/plain",
            },
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        random_val, sig, body = _sign(payload, "test-secret")

        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            # First request -> accepted
            resp1 = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: sig,
                    RANDOM_HEADER: random_val,
                },
            )
            assert resp1.status == 200
            assert (await resp1.json())["status"] == "accepted"

            # Second request with same message but DIFFERENT random value
            # (tests message_id dedup, not replay protection)
            random_val2, sig2, _ = _sign(payload, "test-secret")
            resp2 = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: sig2,
                    RANDOM_HEADER: random_val2,
                },
            )
            assert resp2.status == 200
            assert (await resp2.json())["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_health_endpoint(self, adapter):
        """GET /healthz -> 200 healthy."""
        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "healthy"


class TestConnectValidation:
    """Tests for connect() config validation."""

    @pytest.mark.asyncio
    async def test_connect_with_invalid_port(self):
        """connect() should fail with invalid port."""
        config = MagicMock()
        config.extra = {
            "base_url": "https://nc.example.com",
            "bot_secret": "secret",
            "port": 99999,  # Invalid
        }
        adapter = NextcloudTalkAdapter(config)
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_with_invalid_base_url(self):
        """connect() should fail with invalid base_url."""
        config = MagicMock()
        config.extra = {
            "base_url": "ftp://nc.example.com",  # Not http/https
            "bot_secret": "secret",
            "port": 8745,
        }
        adapter = NextcloudTalkAdapter(config)
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_with_valid_config_fails_no_deps(self):
        """connect() with valid config but missing aiohttp should fail."""
        config = MagicMock()
        config.extra = {
            "base_url": "https://nc.example.com",
            "bot_secret": "secret",
            "port": 8745,
        }
        adapter = NextcloudTalkAdapter(config)

        # Mock AIOHTTP_AVAILABLE as False
        with patch(
            "plugins.platforms.nextcloud_talk.adapter.AIOHTTP_AVAILABLE", False
        ):
            result = await adapter.connect()
            assert result is False


# ── C1: Non-JSON body rejection tests (B6 fix) ─────────────────────────────

class TestNonJsonBody:
    """Tests for non-JSON webhook body rejection (B6 fix)."""

    @pytest.mark.asyncio
    async def test_plain_text_body_rejected(self, adapter):
        """Non-JSON body should be rejected with 400."""
        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            body = "Hello from plain text!"
            random_val = secrets.token_hex(16)
            import hashlib, hmac
            sig = hmac.new(
                b"test-secret",
                (random_val + body).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            resp = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: sig,
                    RANDOM_HEADER: random_val,
                },
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "Body must be valid JSON"

    @pytest.mark.asyncio
    async def test_empty_non_json_body_rejected(self, adapter):
        """Empty non-JSON body: signature passes (raw body), then JSON parse fails (400).

        With raw-body signature verification, a signature over the raw body
        will pass. Then JSON parsing fails and returns 400.
        """
        app = _build_app(adapter)
        async with TestClient(TestServer(app)) as client:
            body = "   "
            random_val = secrets.token_hex(16)
            import hashlib, hmac
            sig = hmac.new(
                b"test-secret",
                (random_val + body).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            resp = await client.post(
                adapter._path,
                data=body,
                headers={
                    SIGNATURE_HEADER: sig,
                    RANDOM_HEADER: random_val,
                },
            )
            # Signature passes (raw body), then JSON parsing fails
            assert resp.status == 400
            data = await resp.json()
            assert "valid JSON" in data["error"]