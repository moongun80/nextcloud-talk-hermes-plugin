"""Unit tests for NextcloudTalkAdapter core methods."""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path (FIRST so our adapter.py takes priority)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Add the Hermes gateway so gateway imports resolve
HERMES_GATEWAY = os.path.expanduser("~/.hermes/hermes-agent")
if HERMES_GATEWAY not in sys.path:
    sys.path.append(HERMES_GATEWAY)

from plugins.platforms.nextcloud_talk.adapter import (
    NextcloudTalkAdapter,
    _json_response,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_adapter(extra: dict | None = None, **env_overrides) -> NextcloudTalkAdapter:
    """Helper to create a NextcloudTalkAdapter with minimal setup."""
    # All env vars this helper may touch — back up and restore all of them
    _ALL_KEYS = (
        "NEXTCLOUD_TALK_BASE_URL",
        "NEXTCLOUD_TALK_BOT_TOKEN",
        "NEXTCLOUD_TALK_BOT_SECRET",
        "NEXTCLOUD_TALK_ALLOWED_USERS",
        "NEXTCLOUD_TALK_GROUP_POLICY",
        "NEXTCLOUD_TALK_DM_POLICY",
        "NEXTCLOUD_TALK_ALLOWED_DM_USERS",
    )
    env_backup = {}
    for key in _ALL_KEYS:
        if key in os.environ:
            env_backup[key] = os.environ[key]

    os.environ["NEXTCLOUD_TALK_BASE_URL"] = "https://nc.example.com"
    os.environ["NEXTCLOUD_TALK_BOT_SECRET"] = "test-secret"

    for k, v in env_overrides.items():
        os.environ[k] = str(v)

    # Use SimpleNamespace so getattr(config, "extra", {}) returns the actual
    # dict value (including empty lists), unlike MagicMock which returns
    # MagicMock for any attribute access.
    config = SimpleNamespace(extra=extra or {})

    adapter = NextcloudTalkAdapter(config)

    # Restore env
    for key in env_backup:
        os.environ[key] = env_backup[key]
    for key in _ALL_KEYS:
        if key not in env_backup:
            os.environ.pop(key, None)

    return adapter


# ── _verify_signature() tests ────────────────────────────────────────────

class TestVerifySignature:
    """Tests for static method _verify_signature()."""

    def test_valid_signature(self):
        """Correct signature should return True."""
        adapter = _make_adapter()
        random_val = "abc123"
        body = "hello world"
        secret = "mysecret"
        import hashlib, hmac
        expected_sig = hmac.new(
            secret.encode(), (random_val + body).encode(), hashlib.sha256
        ).hexdigest()
        assert adapter._verify_signature(random_val, body, secret, expected_sig) is True

    def test_invalid_signature(self):
        """Wrong signature should return False."""
        adapter = _make_adapter()
        assert adapter._verify_signature(
            "abc", "body", "secret", "wrong_signature"
        ) is False

    def test_empty_signature(self):
        """Empty signature should return False."""
        adapter = _make_adapter()
        assert adapter._verify_signature("abc", "body", "secret", "") is False

    def test_case_insensitive_comparison(self):
        """Signature comparison should be case-insensitive."""
        adapter = _make_adapter()
        random_val = "abc123"
        body = "hello"
        secret = "mysecret"
        import hashlib, hmac
        sig_upper = hmac.new(
            secret.encode(), (random_val + body).encode(), hashlib.sha256
        ).hexdigest().upper()
        assert adapter._verify_signature(
            random_val, body, secret, sig_upper
        ) is True


# ── _is_duplicate() tests ────────────────────────────────────────────────

class TestIsDuplicate:
    """Tests for _is_duplicate() deduplication."""

    def test_first_call_not_duplicate(self):
        """First call for a message_id should return False."""
        adapter = _make_adapter()
        assert adapter._is_duplicate("msg-001") is False

    def test_second_call_is_duplicate(self):
        """Second call for same message_id should return True."""
        adapter = _make_adapter()
        assert adapter._is_duplicate("msg-001") is False
        assert adapter._is_duplicate("msg-001") is True

    def test_different_ids_not_duplicate(self):
        """Different message_ids should not be duplicates."""
        adapter = _make_adapter()
        assert adapter._is_duplicate("msg-001") is False
        assert adapter._is_duplicate("msg-002") is False

    def test_empty_id_not_duplicate(self):
        """Empty message_id should return False."""
        adapter = _make_adapter()
        assert adapter._is_duplicate("") is False
        assert adapter._is_duplicate(None) is False  # type: ignore[arg-type]

    def test_stale_entries_evicted(self):
        """Old entries beyond TTL should be evicted."""
        adapter = _make_adapter()
        # Manually insert a stale entry
        adapter._seen_messages["old-msg"] = time.monotonic() - 600  # 10 min ago
        # Call _is_duplicate to trigger eviction
        adapter._is_duplicate("new-msg")
        # Old entry should be gone
        assert "old-msg" not in adapter._seen_messages


# ── _validate_port() tests ───────────────────────────────────────────────

class TestValidatePort:
    """Tests for static method _validate_port()."""

    def test_valid_port(self):
        assert NextcloudTalkAdapter._validate_port(80) is True
        assert NextcloudTalkAdapter._validate_port(8745) is True
        assert NextcloudTalkAdapter._validate_port(65535) is True

    def test_invalid_ports(self):
        assert NextcloudTalkAdapter._validate_port(0) is False
        assert NextcloudTalkAdapter._validate_port(-1) is False
        assert NextcloudTalkAdapter._validate_port(65536) is False
        assert NextcloudTalkAdapter._validate_port(100000) is False

    def test_non_integer(self):
        assert NextcloudTalkAdapter._validate_port("8080") is False
        assert NextcloudTalkAdapter._validate_port(None) is False  # type: ignore[arg-type]


# ── _validate_base_url() tests ───────────────────────────────────────────

class TestValidateBaseUrl:
    """Tests for static method _validate_base_url()."""

    def test_valid_https(self):
        assert NextcloudTalkAdapter._validate_base_url("https://example.com") is True
        assert NextcloudTalkAdapter._validate_base_url("https://nc.example.com/path") is True

    def test_valid_http(self):
        assert NextcloudTalkAdapter._validate_base_url("http://localhost:8745") is True
        assert NextcloudTalkAdapter._validate_base_url("http://127.0.0.1") is True

    def test_invalid_urls(self):
        assert NextcloudTalkAdapter._validate_base_url("ftp://example.com") is False
        assert NextcloudTalkAdapter._validate_base_url("example.com") is False
        assert NextcloudTalkAdapter._validate_base_url("") is False
        assert NextcloudTalkAdapter._validate_base_url("://invalid") is False
        assert NextcloudTalkAdapter._validate_base_url(None) is False  # type: ignore[arg-type]


# ── _parse_message() tests ───────────────────────────────────────────────

class TestParseMessage:
    """Tests for _parse_message() ActivityPub parsing."""

    def test_parse_create_activity(self, sample_create_payload):
        """Create activity should produce a MessageEvent."""
        adapter = _make_adapter()
        event = adapter._parse_message(sample_create_payload)
        assert event is not None
        assert event.text == "Hello from Nextcloud!"
        assert event.source.user_id == "user123"
        assert event.source.chat_id == "room-token-abc"
        assert event.source.chat_type == "dm"
        assert event.message_id == "msg-001"

    def test_parse_group_activity(self, sample_group_payload):
        """Group chat Create activity should set chat_type='group'."""
        adapter = _make_adapter()
        event = adapter._parse_message(sample_group_payload)
        assert event is not None
        assert event.source.chat_type == "group"
        assert event.source.user_id == "user456"

    def test_update_returns_none(self, sample_update_payload):
        """Update activity should return None."""
        adapter = _make_adapter()
        assert adapter._parse_message(sample_update_payload) is None

    def test_delete_returns_none(self, sample_delete_payload):
        """Delete activity should return None."""
        adapter = _make_adapter()
        assert adapter._parse_message(sample_delete_payload) is None

    def test_empty_content_returns_none(self, sample_empty_content_payload):
        """Empty message content should return None."""
        adapter = _make_adapter()
        assert adapter._parse_message(sample_empty_content_payload) is None

    def test_unknown_activity_type_returns_none(self):
        """Unknown activity type should return None."""
        adapter = _make_adapter()
        assert adapter._parse_message({"type": "Unknown"}) is None


# ── Rate limiting tests ──────────────────────────────────────────────────

class TestRateLimiting:
    """Tests for rate limiting functionality."""

    def test_no_limit_initially(self):
        """No rate limit when there are no failed attempts."""
        adapter = _make_adapter()
        assert adapter._is_rate_limited("192.168.1.1") is False

    def test_record_and_check(self):
        """Recording attempts should increase count."""
        adapter = _make_adapter()
        ip = "10.0.0.1"
        for _ in range(5):
            adapter._record_failed_attempt(ip)
        assert adapter._is_rate_limited(ip) is False  # Below threshold

    def test_rate_limit_triggered(self):
        """10+ failures within window should trigger rate limit."""
        adapter = _make_adapter()
        ip = "10.0.0.2"
        for _ in range(10):
            adapter._record_failed_attempt(ip)
        assert adapter._is_rate_limited(ip) is True

    def test_clear_resets_counter(self):
        """_clear_failed_attempts should reset counter."""
        adapter = _make_adapter()
        ip = "10.0.0.3"
        for _ in range(10):
            adapter._record_failed_attempt(ip)
        assert adapter._is_rate_limited(ip) is True
        adapter._clear_failed_attempts(ip)
        assert adapter._is_rate_limited(ip) is False

    def test_different_ips_independent(self):
        """Rate limiting should be per-IP."""
        adapter = _make_adapter()
        for _ in range(10):
            adapter._record_failed_attempt("ip-a")
        assert adapter._is_rate_limited("ip-a") is True
        assert adapter._is_rate_limited("ip-b") is False


# ── Permission policy tests ──────────────────────────────────────────────

class TestPermissionPolicies:
    """Tests for room-based permission policies."""

    def test_default_allows_everyone(self, sample_create_payload):
        """Default policy 'all' should allow everyone."""
        adapter = _make_adapter()
        event = adapter._parse_message(sample_create_payload)
        assert event is not None
        assert adapter._check_permissions(event) is True

    def test_group_members_policy_restricted(self):
        """group_policy='members' should restrict to allowed_users."""
        adapter = _make_adapter(extra={"group_policy": "members", "allowed_users": ["user123", "user456"]})
        # Create a mock event with an allowed user
        from gateway.session import SessionSource
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="room-abc",
            chat_name="room-abc",
            chat_type="group",
            user_id="user123",
            user_name="Allowed User",
        )
        event = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={},
        )
        assert adapter._check_permissions(event) is True

        # Disallowed user
        source2 = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="room-abc",
            chat_name="room-abc",
            chat_type="group",
            user_id="unknown-user",
            user_name="Unknown User",
        )
        event2 = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source2,
            raw_message={},
        )
        assert adapter._check_permissions(event2) is False

    def test_dm_restricted_policy(self):
        """dm_policy='restricted' should restrict to allowed_dm_users."""
        adapter = _make_adapter(extra={
            "dm_policy": "restricted",
            "allowed_dm_users": ["trusted-user"],
        })
        from gateway.session import SessionSource
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="dm-abc",
            chat_name="dm-abc",
            chat_type="dm",
            user_id="trusted-user",
            user_name="Trusted",
        )
        event = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={},
        )
        assert adapter._check_permissions(event) is True

        source2 = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="dm-abc",
            chat_name="dm-abc",
            chat_type="dm",
            user_id="stranger",
            user_name="Stranger",
        )
        event2 = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source2,
            raw_message={},
        )
        assert adapter._check_permissions(event2) is False

    def test_group_policy_doesnt_apply_to_dm(self):
        """group_policy should not affect DM messages."""
        adapter = _make_adapter(extra={
            "group_policy": "members",
            "allowed_users": [],
        })
        from gateway.session import SessionSource
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="dm-xyz",
            chat_name="dm-xyz",
            chat_type="dm",
            user_id="random-user",
            user_name="Random",
        )
        event = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={},
        )
        # DM should be allowed even with empty allowed_users for groups
        assert adapter._check_permissions(event) is True

    def test_group_members_policy_empty_whitelist_blocks_all(self):
        """group_policy='members' + empty allowed_users should block everyone in groups."""
        adapter = _make_adapter(extra={
            "group_policy": "members",
            "allowed_users": [],
        })
        from gateway.session import SessionSource
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="room-xyz",
            chat_name="room-xyz",
            chat_type="group",
            user_id="anyone",
            user_name="Anyone",
        )
        event = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={},
        )
        # Empty whitelist → deny all
        assert adapter._check_permissions(event) is False

    def test_dm_restricted_empty_whitelist_blocks_all(self):
        """dm_policy='restricted' + empty allowed_dm_users should block all DMs."""
        adapter = _make_adapter(extra={
            "dm_policy": "restricted",
            "allowed_dm_users": [],
        })
        from gateway.session import SessionSource
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType

        source = SessionSource(
            platform=Platform("nextcloud_talk"),
            chat_id="dm-xyz",
            chat_name="dm-xyz",
            chat_type="dm",
            user_id="anyone",
            user_name="Anyone",
        )
        event = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={},
        )
        # Empty whitelist → deny all
        assert adapter._check_permissions(event) is False

    def test_env_var_allowed_users(self):
        """allowed_users from environment variable should be parsed correctly."""
        adapter = _make_adapter(
            extra={},
            NEXTCLOUD_TALK_ALLOWED_USERS="alice,bob,charlie",
        )
        assert adapter._allowed_users == {"alice", "bob", "charlie"}

    def test_env_var_group_policy(self):
        """group_policy from environment variable should be used."""
        adapter = _make_adapter(
            extra={},
            NEXTCLOUD_TALK_GROUP_POLICY="members",
        )
        assert adapter._group_policy == "members"

    def test_env_var_dm_policy(self):
        """dm_policy from environment variable should be used."""
        adapter = _make_adapter(
            extra={},
            NEXTCLOUD_TALK_DM_POLICY="restricted",
        )
        assert adapter._dm_policy == "restricted"