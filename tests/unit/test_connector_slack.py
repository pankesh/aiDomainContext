"""Unit tests for the Slack connector."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aidomaincontext.connectors.slack import SlackConnector
from tests.fixtures.slack_responses import (
    AUTH_TEST_RESPONSE,
    CONVERSATIONS_HISTORY_RESPONSE,
    CONVERSATIONS_INFO_RESPONSE,
    CONVERSATIONS_LIST_RESPONSE,
    CONVERSATIONS_REPLIES_RESPONSE,
    SLACK_EVENT_BOT_MESSAGE,
    SLACK_EVENT_MESSAGE,
    SLACK_EVENT_THREAD_REPLY,
)


@pytest.fixture
def connector():
    return SlackConnector()


@pytest.fixture
def config():
    return {"bot_token": "xoxb-test-token-123", "channels": ["C001"]}


# ------------------------------------------------------------------ #
# connector_type
# ------------------------------------------------------------------ #


def test_connector_type(connector):
    assert connector.connector_type == "slack"


# ------------------------------------------------------------------ #
# validate_credentials
# ------------------------------------------------------------------ #


def _make_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=json_data, request=httpx.Request("GET", "https://slack.com/api/auth.test"))


@pytest.mark.asyncio
async def test_validate_credentials_success(connector, config):
    mock_response = _make_response(AUTH_TEST_RESPONSE)

    with patch("aidomaincontext.connectors.slack.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_response)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is True


@pytest.mark.asyncio
async def test_validate_credentials_empty_token(connector):
    result = await connector.validate_credentials({"bot_token": ""})
    assert result is False


@pytest.mark.asyncio
async def test_validate_credentials_missing_token(connector):
    result = await connector.validate_credentials({})
    assert result is False


@pytest.mark.asyncio
async def test_validate_credentials_api_error(connector, config):
    error_response = _make_response({"ok": False, "error": "invalid_auth"})

    with patch("aidomaincontext.connectors.slack.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=error_response)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is False


# ------------------------------------------------------------------ #
# fetch_documents — explicit channel list
# ------------------------------------------------------------------ #


def _build_mock_client(url_to_response: dict[str, httpx.Response]) -> AsyncMock:
    """Create a mock httpx.AsyncClient whose .get() returns responses based on URL substring."""

    async def mock_get(url, *, headers=None, params=None):
        for key, resp in url_to_response.items():
            if key in str(url):
                return resp
        raise AssertionError(f"Unexpected URL: {url}")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=mock_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_fetch_documents_explicit_channels(connector, config):
    """With explicit channel IDs, the connector should fetch info + history + replies."""
    url_map = {
        "conversations.info": _make_response(CONVERSATIONS_INFO_RESPONSE),
        "conversations.history": _make_response(CONVERSATIONS_HISTORY_RESPONSE),
        "conversations.replies": _make_response(CONVERSATIONS_REPLIES_RESPONSE),
    }

    with patch("aidomaincontext.connectors.slack.httpx.AsyncClient") as MockClient:
        MockClient.return_value = _build_mock_client(url_map)

        docs = []
        async for doc, cursor in connector.fetch_documents(config, None):
            docs.append((doc, cursor))

        # CONVERSATIONS_HISTORY_RESPONSE has 3 messages; the one with subtype should be skipped.
        # Message 1 (reply_count=2) -> fetched with thread replies
        # Message 2 (no replies) -> plain message
        assert len(docs) == 2

        doc0, cursor0 = docs[0]
        assert doc0.source_type == "slack_message"
        assert doc0.source_id == "slack:C001:1710000001.000001"
        assert "Thread Replies" in doc0.content
        assert doc0.author == "U100"
        assert doc0.metadata["channel"] == "C001"
        assert doc0.metadata["channel_name"] == "general"
        assert "last_sync_ts" in cursor0

        doc1, _ = docs[1]
        assert doc1.source_id == "slack:C001:1710000002.000002"
        assert "Thread Replies" not in doc1.content


@pytest.mark.asyncio
async def test_fetch_documents_discover_channels(connector):
    """With channels=None, the connector should discover channels via conversations.list."""
    config_no_channels = {"bot_token": "xoxb-test-token-123"}

    url_map = {
        "conversations.list": _make_response(CONVERSATIONS_LIST_RESPONSE),
        "conversations.history": _make_response(CONVERSATIONS_HISTORY_RESPONSE),
        "conversations.replies": _make_response(CONVERSATIONS_REPLIES_RESPONSE),
    }

    with patch("aidomaincontext.connectors.slack.httpx.AsyncClient") as MockClient:
        MockClient.return_value = _build_mock_client(url_map)

        docs = []
        async for doc, cursor in connector.fetch_documents(config_no_channels, None):
            docs.append(doc)

        # 2 channels x 2 non-subtype messages each = 4 documents
        assert len(docs) == 4
        source_types = {d.source_type for d in docs}
        assert source_types == {"slack_message"}


@pytest.mark.asyncio
async def test_fetch_documents_incremental_cursor(connector, config):
    """Cursor dict should be forwarded as the 'oldest' parameter."""
    cursor_input = {"last_sync_ts": "1710000001.500000"}

    url_map = {
        "conversations.info": _make_response(CONVERSATIONS_INFO_RESPONSE),
        "conversations.history": _make_response(CONVERSATIONS_HISTORY_RESPONSE),
        "conversations.replies": _make_response(CONVERSATIONS_REPLIES_RESPONSE),
    }

    with patch("aidomaincontext.connectors.slack.httpx.AsyncClient") as MockClient:
        mock_client = _build_mock_client(url_map)
        MockClient.return_value = mock_client

        docs = []
        async for doc, cursor in connector.fetch_documents(config, cursor_input):
            docs.append((doc, cursor))

        # Still produces docs (the mock doesn't filter by oldest), but we verify
        # the cursor was accepted without error and new cursor is updated.
        assert len(docs) > 0
        _, last_cursor = docs[-1]
        assert "last_sync_ts" in last_cursor


# ------------------------------------------------------------------ #
# handle_webhook
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_handle_webhook_message(connector):
    docs = await connector.handle_webhook(SLACK_EVENT_MESSAGE)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_type == "slack_message"
    assert doc.source_id == "slack:C001:1710000099.000099"
    assert doc.content == "A new message via webhook"
    assert doc.author == "U100"


@pytest.mark.asyncio
async def test_handle_webhook_thread_reply(connector):
    docs = await connector.handle_webhook(SLACK_EVENT_THREAD_REPLY)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_id == "slack:C001:1710000100.000100"
    # Thread reply metadata should reference the parent ts
    assert doc.metadata["thread_ts"] == "1710000099.000099"


@pytest.mark.asyncio
async def test_handle_webhook_bot_message_ignored(connector):
    """Messages with a subtype (e.g. bot_message) should be ignored."""
    docs = await connector.handle_webhook(SLACK_EVENT_BOT_MESSAGE)
    assert docs == []


@pytest.mark.asyncio
async def test_handle_webhook_non_message_event(connector):
    """Non-message events should be ignored."""
    payload = {
        "type": "event_callback",
        "event": {"type": "reaction_added", "user": "U100"},
    }
    docs = await connector.handle_webhook(payload)
    assert docs == []


@pytest.mark.asyncio
async def test_handle_webhook_empty_payload(connector):
    docs = await connector.handle_webhook({})
    assert docs == []


# ------------------------------------------------------------------ #
# Document shape validation
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_document_shape(connector):
    """Verify all required DocumentBase fields are set on webhook-produced docs."""
    docs = await connector.handle_webhook(SLACK_EVENT_MESSAGE)
    doc = docs[0]

    assert isinstance(doc.source_id, str)
    assert isinstance(doc.source_type, str)
    assert isinstance(doc.title, str)
    assert isinstance(doc.content, str)
    assert doc.url is not None
    assert isinstance(doc.metadata, dict)
    assert "channel" in doc.metadata
