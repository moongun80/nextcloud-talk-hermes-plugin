"""Comprehensive tests for all ANALYST report fixes (C1-C3, M1-M8, m1-m10)."""

from __future__ import annotations

import os
import sys
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

HERMES_GATEWAY = os.path.expanduser("~/.hermes/hermes-agent")
if HERMES_GATEWAY not in sys.path:
    sys.path.append(HERMES_GATEWAY)

from plugins.platforms.nextcloud_talk.adapter import (
    NextcloudTalkAdapter,
    _standalone_send,
    _json_response,
    _DEFAULT_TRUSTED_PROXIES,
    _check_requirements_state,
    SEND_RANDOM_HEADER,
    SEND_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    RANDOM_HEADER,
    check_requirements,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_adapter(extra: dict | None = None, **env_overrides) -> NextcloudTalkAdapter:
    """Helper to create a NextcloudTalkAdapter with minimal setup."""
    _ALL_KEYS = (
        "NEXTCLOUD_TALK_BASE_URL",
        "NEXTCLOUD_TALK_BOT_SECRET",
        "NEXTCLOUD_TALK_ALLOWED_USERS",
        "NEXTCLOUD_TALK_GROUP_POLICY",
        "NEXTCLOUD_TALK_DM_POLICY",
        "NEXTCLOUD_TALK_ALLOWED_DM_USERS",
        "NEXTCLOUD_TALK_TRUSTED_PROXIES",
        "NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH",
    )
    env_backup = {}
    for key in _ALL_KEYS:
        if key in os.environ:
            env_backup[key] = os.environ[key]

    os.environ["NEXTCLOUD_TALK_BASE_URL"] = "https://nc.example.com"
    os.environ["NEXTCLOUD_TALK_BOT_SECRET"] = "test-secret"

    for k, v in env_overrides.items():
        os.environ[k] = str(v)

    config = SimpleNamespace(extra=extra or {})
    adapter = NextcloudTalkAdapter(config)

    # Restore env
    for key in env_backup:
        os.environ[key] = env_backup[key]
    for key in _ALL_KEYS:
        if key not in env_backup:
            os.environ.pop(key, None)

    return adapter


@pytest.fixture(autouse=True)
def _reset_trusted_proxies():
    """Reset _DEFAULT_TRUSTED_PROXIES before each test."""
    import plugins.platforms.nextcloud_talk.adapter as adapter_mod
    adapter_mod._DEFAULT_TRUSTED_PROXIES = set()
    yield
    import plugins.platforms.nextcloud_talk.adapter as adapter_mod
    adapter_mod._DEFAULT_TRUSTED_PROXIES = set()


@pytest.fixture(autouse=True)
def _reset_check_requirements_state():
    """Reset module-level state before each test."""
    _check_requirements_state._has_config = False
    _check_requirements_state._has_secret = False
    _check_requirements_state._has_base_url = False
    yield
    _check_requirements_state._has_config = False
    _check_requirements_state._has_secret = False
    _check_requirements_state._has_base_url = False


# ── C1: object.content JSON parsing tests ─────────────────────────────────

class TestC1ContentParsing:
    """Tests for C1: Parse object.content as JSON with plain-text fallback."""

    def test_json_content_with_message(self):
        """JSON content with message field should extract the message."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {
                "type": "Note", "id": "m1",
                "content": json.dumps({"message": "Hello world", "parameters": {}}),
            },
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.text == "Hello world"

    def test_json_content_with_parameters_rendered(self):
        """JSON content with parameters should render placeholders."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {
                "type": "Note", "id": "m1",
                "content": json.dumps({
                    "message": "hi {mention-1}!",
                    "parameters": {"mention-1": {"displayName": "Alice"}},
                }),
            },
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.text == "hi Alice!"

    def test_plain_text_content_fallback(self):
        """Plain text content should be used directly when not JSON."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {
                "type": "Note", "id": "m1",
                "content": "Just plain text",
            },
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.text == "Just plain text"

    def test_empty_content_returns_none(self):
        """Empty content should return None."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": ""},
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        assert adapter._parse_message(data) is None

    def test_static_parse_content_directly(self):
        """Test _parse_content static method directly."""
        # Valid JSON with message
        assert NextcloudTalkAdapter._parse_content(
            json.dumps({"message": "test", "parameters": {}})
        ) == "test"

        # Invalid JSON falls back to plain text
        assert NextcloudTalkAdapter._parse_content("plain text") == "plain text"

        # Empty string
        assert NextcloudTalkAdapter._parse_content("") == ""

        # JSON without message key
        assert NextcloudTalkAdapter._parse_content(json.dumps({"foo": "bar"})) == ""

        # JSON with null message
        assert NextcloudTalkAdapter._parse_content(json.dumps({"message": None})) == ""


# ── C2: Send headers tests ────────────────────────────────────────────────

class TestC2SendHeaders:
    """Tests for C2: Send uses -Bot- variant headers."""

    @pytest.mark.asyncio
    async def test_send_uses_bot_headers(self):
        """send() must use SEND_RANDOM_HEADER and SEND_SIGNATURE_HEADER."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._running = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post = AsyncMock(return_value=mock_resp)
        with patch.object(adapter._http_client, 'post', mock_post):
            await adapter.send("roomabc", "Hello")

            headers_used = mock_post.call_args.kwargs["headers"]
            assert SEND_RANDOM_HEADER in headers_used
            assert SEND_SIGNATURE_HEADER in headers_used
            # Should NOT use receive headers for sending
            assert RANDOM_HEADER not in headers_used
            assert SIGNATURE_HEADER not in headers_used

    @pytest.mark.asyncio
    async def test_header_constants_are_correct(self):
        """Header constant values must match Nextcloud Bot API spec."""
        assert SEND_RANDOM_HEADER == "x-nextcloud-talk-bot-random"
        assert SEND_SIGNATURE_HEADER == "x-nextcloud-talk-bot-signature"
        assert SIGNATURE_HEADER == "x-nextcloud-talk-signature"
        assert RANDOM_HEADER == "x-nextcloud-talk-random"


# ── C3: replyTo int conversion tests ──────────────────────────────────────

class TestC3ReplyToInt:
    """Tests for C3: replyTo integer conversion."""

    @pytest.mark.asyncio
    async def test_replyto_converted_to_int(self):
        """String reply_to should be converted to int in JSON body."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._running = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post = AsyncMock(return_value=mock_resp)
        with patch.object(adapter._http_client, 'post', mock_post):
            await adapter.send("roomabc", "Hello", reply_to="12345")

            body = mock_post.call_args.kwargs["content"].decode("utf-8")
            body_obj = json.loads(body)
            assert body_obj["message"] == "Hello"
            assert body_obj["replyTo"] == 12345

    @pytest.mark.asyncio
    async def test_invalid_replyto_omitted(self):
        """Non-numeric reply_to should be logged and omitted."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._running = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post = AsyncMock(return_value=mock_resp)
        with patch.object(adapter._http_client, 'post', mock_post):
            await adapter.send("roomabc", "Hello", reply_to="not-a-number")

            body = mock_post.call_args.kwargs["content"].decode("utf-8")
            body_obj = json.loads(body)
            assert body_obj["message"] == "Hello"
            assert "replyTo" not in body_obj


# ── M1: Chat type detection from target.type ──────────────────────────────

class TestM1ChatTypeDetection:
    """Tests for M1: chat_type determined from isGroupChat legacy field."""

    def test_legacy_isGroupChat_true_gives_group(self):
        """isGroupChat=True should give chat_type='group'."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
            "isGroupChat": True,
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.source.chat_type == "group"

    def test_legacy_isGroupChat_false_gives_dm(self):
        """isGroupChat=False should give chat_type='dm'."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
            "isGroupChat": False,
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.source.chat_type == "dm"

    def test_missing_isGroupChat_defaults_to_dm(self):
        """Missing isGroupChat should default to 'dm'."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "room1", "name": "room1"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.source.chat_type == "dm"


# ── M2: target.id simplification ──────────────────────────────────────────

class TestM2TargetId:
    """Tests for M2: target.id is the room token directly."""

    def test_simple_token_used_as_room(self):
        """Simple token should be used directly as room_token."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "n3xtc10ud", "name": "My Room"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.source.chat_id == "n3xtc10ud"

    def test_room_name_preserved(self):
        """target.name should be used as chat_name."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "n3xtc10ud", "name": "Engineering Chat"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.source.chat_name == "Engineering Chat"

    def test_missing_target_name_falls_back_to_token(self):
        """Missing target.name should fall back to room token."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "n3xtc10ud"},
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.source.chat_name == "n3xtc10ud"

    def test_old_call_prefix_handling_removed(self):
        """The old 'call/' prefix branch should be gone."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "u1", "name": "User"},
            "object": {"type": "Note", "id": "m1", "content": "hi"},
            "target": {"type": "Collection", "id": "call/n3xtc10ud", "name": "Call Room"},
        }
        # Since target.id is now used directly, "call/n3xtc10ud" would be the token.
        # The old code would have extracted "n3xtc10ud". This is acceptable since
        # the spec says target.id IS the token.
        event = adapter._parse_message(data)
        assert event is not None
        # The token includes the full target.id value (spec compliance)
        assert event.source.chat_id == "call/n3xtc10ud"


# ── M3: Replay protection tests ───────────────────────────────────────────

class TestM3ReplayProtection:
    """Tests for M3: Replay protection via seen_randoms."""

    def test_first_random_accepted(self):
        """First occurrence of a random value should not be flagged."""
        adapter = _make_adapter()
        assert adapter._is_replayed_random("unique-random-1") is False

    def test_duplicate_random_rejected(self):
        """Same random value used twice should be rejected."""
        adapter = _make_adapter()
        assert adapter._is_replayed_random("reuse-random") is False
        assert adapter._is_replayed_random("reuse-random") is True

    def test_different_randoms_accepted(self):
        """Different random values should all be accepted."""
        adapter = _make_adapter()
        assert adapter._is_replayed_random("rand-1") is False
        assert adapter._is_replayed_random("rand-2") is False
        assert adapter._is_replayed_random("rand-3") is False

    def test_stale_randoms_evicted(self):
        """Old random values beyond TTL should be evicted."""
        adapter = _make_adapter()
        # Manually insert a stale entry
        adapter._seen_randoms["old-random"] = time.monotonic() - 600  # 10 min ago
        # Use a new random to trigger eviction
        assert adapter._is_replayed_random("new-random") is False
        assert "old-random" not in adapter._seen_randoms


# ── M4: check_requirements with config.yaml support ───────────────────────

class TestM4CheckRequirements:
    """Tests for M4: check_requirements reads from config extra."""

    def test_config_yaml_only_setup(self):
        """check_requirements should pass when config.yaml has credentials."""
        adapter = _make_adapter(extra={
            "base_url": "https://nc.example.com",
            "bot_secret": "config-secret",
        })
        assert adapter._bot_secret == "config-secret"
        assert adapter._base_url == "https://nc.example.com"
        # Module-level state should be set
        assert _check_requirements_state._has_config is True
        assert _check_requirements_state._has_secret is True
        assert _check_requirements_state._has_base_url is True
        assert check_requirements() is True

    def test_env_var_only_setup(self):
        """check_requirements should pass with env vars only."""
        # Remove module-level state
        _check_requirements_state._has_config = False
        adapter = _make_adapter(extra={})
        assert check_requirements() is True

    def test_neither_config_nor_env(self):
        """check_requirements should fail with neither config nor env."""
        _check_requirements_state._has_config = False
        _check_requirements_state._has_secret = False
        _check_requirements_state._has_base_url = False
        assert check_requirements() is False


# ── M5: X-Forwarded-For tests ─────────────────────────────────────────────

class TestM5XForwardedFor:
    """Tests for M5: X-Forwarded-For with trusted proxy support."""

    def test_no_proxy_returns_remote(self):
        """Without XFF, should use request.remote."""
        adapter = _make_adapter()
        mock_request = MagicMock()
        mock_request.remote = "192.168.1.1"
        mock_request.headers.get.return_value = None
        assert adapter._get_client_ip(mock_request) == "192.168.1.1"

    def test_trusted_proxy_chain(self):
        """With trusted proxies, should use last untrusted IP."""
        import plugins.platforms.nextcloud_talk.adapter as adapter_mod
        adapter_mod._DEFAULT_TRUSTED_PROXIES = {"10.0.0.0/8"}

        adapter = _make_adapter()
        mock_request = MagicMock()
        mock_request.remote = "10.0.0.1"
        mock_request.headers.get.side_effect = lambda key, default=None: {
            "X-Forwarded-For": "203.0.113.50, 10.0.0.1"
        }.get(key, default)

        # 203.0.113.50 is untrusted, 10.0.0.1 is trusted
        assert adapter._get_client_ip(mock_request) == "203.0.113.50"

    def test_no_trusted_proxies_ignores_xff(self):
        """Without trusted proxies, XFF must be ignored to prevent spoofing."""
        import plugins.platforms.nextcloud_talk.adapter as adapter_mod
        adapter_mod._DEFAULT_TRUSTED_PROXIES = set()

        adapter = _make_adapter()
        mock_request = MagicMock()
        mock_request.remote = "10.0.0.1"
        mock_request.headers.get.side_effect = lambda key, default=None: {
            "X-Forwarded-For": "203.0.113.50, 10.0.0.1"
        }.get(key, default)

        # XFF must be ignored; should return request.remote
        assert adapter._get_client_ip(mock_request) == "10.0.0.1"

    def test_no_trusted_proxies_xff_with_no_remote(self):
        """Without trusted proxies and no remote, return 0.0.0.0."""
        import plugins.platforms.nextcloud_talk.adapter as adapter_mod
        adapter_mod._DEFAULT_TRUSTED_PROXIES = set()

        adapter = _make_adapter()
        mock_request = MagicMock()
        mock_request.remote = None
        mock_request.headers.get.side_effect = lambda key, default=None: {
            "X-Forwarded-For": "203.0.113.50"
        }.get(key, default)

        assert adapter._get_client_ip(mock_request) == "0.0.0.0"

    def test_unknown_remote_fallback(self):
        """When remote is None and no XFF, should return 0.0.0.0."""
        adapter = _make_adapter()
        mock_request = MagicMock()
        mock_request.remote = None
        mock_request.headers.get.return_value = None
        assert adapter._get_client_ip(mock_request) == "0.0.0.0"





# ── m5: Room token sanitization ───────────────────────────────────────────

class TestM5RoomTokenSanitization:
    """Tests for m5: Room token URL path sanitization."""

    @pytest.mark.asyncio
    async def test_safe_token_accepted(self):
        """Lowercase alphanumeric tokens should work."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._running = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post = AsyncMock(return_value=mock_resp)
        with patch.object(adapter._http_client, 'post', mock_post):
            result = await adapter.send("safetoken123", "Hello")
            assert result.success is True

    @pytest.mark.asyncio
    async def test_unsafe_token_rejected(self):
        """Tokens with path traversal chars should be rejected."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._running = True

        result = await adapter.send("../etc/passwd", "Hello")
        assert result.success is False
        assert "Invalid" in result.error

    @pytest.mark.asyncio
    async def test_spaces_in_token_rejected(self):
        """Tokens with spaces should be rejected."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._running = True

        result = await adapter.send("room with spaces", "Hello")
        assert result.success is False


# ── m9: Max message length ────────────────────────────────────────────────

class TestM9MaxMessageLength:
    """Tests for m9: Max message length enforcement."""

    def test_default_max_length(self):
        """Default max_message_length should be 32000."""
        adapter = _make_adapter()
        assert adapter._max_message_length == 32000

    def test_custom_max_length_from_config(self):
        """Custom max_message_length from config should be used."""
        adapter = _make_adapter(extra={"max_message_length": 1000})
        assert adapter._max_message_length == 1000

    def test_custom_max_length_from_env(self):
        """Custom max_message_length from env should be used."""
        adapter = _make_adapter(extra={}, NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH="5000")
        assert adapter._max_message_length == 5000

    @pytest.mark.asyncio
    async def test_long_message_truncated(self):
        """Messages exceeding max length should be truncated."""
        adapter = _make_adapter(extra={"max_message_length": 10})
        adapter._http_client = MagicMock()
        adapter._running = True

        long_msg = "x" * 100
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post = AsyncMock(return_value=mock_resp)
        with patch.object(adapter._http_client, 'post', mock_post):
            await adapter.send("roomabc", long_msg)

            body = mock_post.call_args.kwargs["content"].decode("utf-8")
            body_obj = json.loads(body)
            # JSON body with truncated message
            assert body_obj["message"] == "x" * 10
            assert len(body_obj["message"]) == 10


# ── Integration: Full message flow ────────────────────────────────────────

class TestIntegration:
    """End-to-end integration tests for the full message flow."""

    def test_full_json_message_flow(self):
        """Full flow: JSON content -> parse -> correct fields."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "user123", "name": "Alice"},
            "object": {
                "type": "Note", "id": "msg-001",
                "content": json.dumps({
                    "message": "Hello {mention-1}!",
                    "parameters": {"mention-1": {"displayName": "Bob"}}
                }),
            },
            "target": {"type": "Collection", "id": "grp-abc", "name": "Team Chat"},
            "isGroupChat": True,
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.text == "Hello Bob!"
        assert event.source.user_id == "user123"
        assert event.source.user_name == "Alice"
        assert event.source.chat_id == "grp-abc"
        assert event.source.chat_name == "Team Chat"
        assert event.source.chat_type == "group"
        assert event.message_id == "msg-001"

    def test_full_dm_message_flow(self):
        """Full flow for DM messages."""
        adapter = _make_adapter()
        data = {
            "type": "Create",
            "actor": {"type": "Person", "id": "user456", "name": "Charlie"},
            "object": {
                "type": "Note", "id": "msg-002",
                "content": "Direct message here",
            },
            "target": {"type": "Collection", "id": "dm-xyz", "name": "DM"},
            "isGroupChat": False,
        }
        event = adapter._parse_message(data)
        assert event is not None
        assert event.text == "Direct message here"
        assert event.source.chat_type == "dm"
        assert event.source.chat_name == "DM"


# ── check_requirements integration ────────────────────────────────────────

class TestCheckRequirementsIntegration:
    """Integration tests for check_requirements with various configs."""

    def test_config_only_no_env(self):
        """check_requirements passes with config.yaml only (no env vars)."""
        # Clear env vars
        for key in ["NEXTCLOUD_TALK_BASE_URL", "NEXTCLOUD_TALK_BOT_SECRET"]:
            os.environ.pop(key, None)

        adapter = _make_adapter(extra={
            "base_url": "https://nc.example.com",
            "bot_secret": "config-secret",
        })
        assert check_requirements() is True

    def test_both_config_and_env(self):
        """check_requirements passes with both config and env."""
        adapter = _make_adapter(extra={
            "base_url": "https://nc.example.com",
            "bot_secret": "config-secret",
        }, NEXTCLOUD_TALK_BASE_URL="https://env.example.com")
        assert check_requirements() is True
# ── B2: max_message_length is NOT passed to register() ────────────────────
# The register_platform() call does NOT include max_message_length.
# The adapter reads it from config/env in __init__ only.

class TestB2RegisterMaxMessageLength:
    """Tests verifying max_message_length is NOT in register_platform() call."""

    def test_register_does_not_include_max_message_length(self):
        """register() must NOT pass max_message_length (not a valid PlatformEntry field)."""
        os.environ.pop("NEXTCLOUD_TALK_MAX_MESSAGE_LENGTH", None)

        mock_ctx = MagicMock()
        from plugins.platforms.nextcloud_talk.adapter import register

        register(mock_ctx)

        call_kwargs = mock_ctx.register_platform.call_args.kwargs
        assert "max_message_length" not in call_kwargs
