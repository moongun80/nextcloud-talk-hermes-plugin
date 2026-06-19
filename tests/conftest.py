"""Pytest configuration for nextcloud-talk-hermes-plugin tests."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, AsyncMock

import pytest

# Ensure the project root is on sys.path so we can import the adapter module
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Add the Hermes gateway so gateway imports resolve (append, not prepend,
# so the project's own adapter.py takes priority over hermes-agent's copy)
HERMES_GATEWAY = os.path.expanduser("~/.hermes/hermes-agent")
if HERMES_GATEWAY not in sys.path:
    sys.path.append(HERMES_GATEWAY)


def pytest_configure(config):
    """Ensure sys.path is set before any imports."""
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    if HERMES_GATEWAY not in sys.path:
        sys.path.append(HERMES_GATEWAY)


# ── Fake config object ────────────────────────────────────────────────────

@pytest.fixture
def fake_config():
    """Create a minimal config-like object for NextcloudTalkAdapter."""
    config = MagicMock()
    config.extra = {
        "base_url": "https://nc.example.com",
        "bot_secret": "test-secret",
        "host": "127.0.0.1",
        "port": 9999,
        "path": "/test-callback",
    }
    return config


# ── Mock aiohttp response helper ──────────────────────────────────────────

@pytest.fixture
def mock_aiohttp_response():
    """Create a mock aiohttp web.Response."""
    response = MagicMock()
    response.status = 200
    response.text = '{"status":"ok"}'
    response.content_type = "application/json"
    return response


# ── Sample webhook payloads ───────────────────────────────────────────────

@pytest.fixture
def sample_create_payload():
    """Sample ActivityPub Create payload (new message)."""
    return {
        "type": "Create",
        "actor": {"type": "Person", "id": "user123", "name": "Test User"},
        "object": {
            "type": "Note",
            "id": "msg-001",
            "name": "",
            "content": "Hello from Nextcloud!",
            "mediaType": "text/plain",
        },
        "target": {"type": "Collection", "id": "room-token-abc", "name": "room-token-abc"},
        "isGroupChat": False,
    }


@pytest.fixture
def sample_group_payload():
    """Sample group chat payload."""
    return {
        "type": "Create",
        "actor": {"type": "Person", "id": "user456", "name": "Group Member"},
        "object": {
            "type": "Note",
            "id": "msg-002",
            "name": "",
            "content": "Group message here",
            "mediaType": "text/plain",
        },
        "target": {"type": "Collection", "id": "group-room-xyz", "name": "group-room-xyz"},
        "isGroupChat": True,
    }


@pytest.fixture
def sample_update_payload():
    """Sample ActivityPub Update payload (edited message)."""
    return {
        "type": "Update",
        "actor": {"type": "Person", "id": "user123", "name": "Test User"},
        "object": {
            "type": "Note",
            "id": "msg-001",
            "content": "Edited message",
        },
        "target": {"type": "Collection", "id": "room-token-abc", "name": "room-token-abc"},
    }


@pytest.fixture
def sample_delete_payload():
    """Sample ActivityPub Delete payload (deleted message)."""
    return {
        "type": "Delete",
        "actor": {"type": "Person", "id": "user123", "name": "Test User"},
        "object": {
            "type": "Note",
            "id": "msg-001",
        },
        "target": {"type": "Collection", "id": "room-token-abc", "name": "room-token-abc"},
    }


@pytest.fixture
def sample_empty_content_payload():
    """Payload with empty message content."""
    return {
        "type": "Create",
        "actor": {"type": "Person", "id": "user123", "name": "Test User"},
        "object": {
            "type": "Note",
            "id": "msg-003",
            "content": "",
        },
        "target": {"type": "Collection", "id": "room-token-abc", "name": "room-token-abc"},
    }