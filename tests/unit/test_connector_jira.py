"""Unit tests for the Jira connector."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aidomaincontext.connectors.jira import JiraConnector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def connector():
    return JiraConnector()


@pytest.fixture
def config():
    return {
        "email": "alice@example.com",
        "api_token": "ATATT3xFfGF0testtoken",
        "domain": "acme.atlassian.net",
        "project_keys": ["PROJ"],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://acme.atlassian.net/rest/api/3/search"),
    )


def _make_issue(
    key: str = "PROJ-1",
    summary: str = "Test issue",
    status: str = "Open",
    description: str = "Issue description",
    comment_count: int = 0,
) -> dict:
    comments = []
    for i in range(comment_count):
        comments.append(
            {
                "author": {"displayName": f"user{i}"},
                "body": f"Comment {i} body",
                "renderedBody": f"<p>Comment {i} body</p>",
            }
        )
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "renderedFields": {"description": f"<p>{description}</p>"},
            "status": {"name": status},
            "priority": {"name": "Medium"},
            "issuetype": {"name": "Story"},
            "project": {"key": key.split("-")[0]},
            "assignee": {"displayName": "Bob"},
            "reporter": {"displayName": "Alice", "emailAddress": "alice@example.com"},
            "comment": {"comments": comments},
        },
    }


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == "jira"


# ---------------------------------------------------------------------------
# validate_credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_credentials_success(connector, config):
    mock_resp = _make_response({"accountId": "abc123", "displayName": "Alice"})

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is True


@pytest.mark.asyncio
async def test_validate_credentials_bad_token(connector, config):
    mock_resp = _make_response({"message": "Unauthorized"}, status_code=401)

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is False


@pytest.mark.asyncio
async def test_validate_credentials_missing_fields(connector):
    result = await connector.validate_credentials({"email": "test@example.com"})
    assert result is False


@pytest.mark.asyncio
async def test_validate_credentials_network_error(connector, config):
    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is False


# ---------------------------------------------------------------------------
# fetch_documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_documents_basic(connector, config):
    issue = _make_issue("PROJ-1", "Login bug", comment_count=2)
    search_resp = _make_response({"issues": [issue], "total": 1, "startAt": 0})

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=search_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        docs = []
        async for doc, cursor in connector.fetch_documents(config, None):
            docs.append((doc, cursor))

    assert len(docs) == 1
    doc, cursor = docs[0]

    assert doc.source_type == "jira_issue"
    assert doc.source_id == "jira:acme.atlassian.net:PROJ-1"
    assert doc.title == "Login bug"
    assert doc.url == "https://acme.atlassian.net/browse/PROJ-1"
    assert doc.author == "Alice"
    assert doc.metadata["issue_key"] == "PROJ-1"
    assert doc.metadata["project_key"] == "PROJ"
    assert doc.metadata["status"] == "Open"
    assert "last_sync_at" in cursor


@pytest.mark.asyncio
async def test_fetch_documents_includes_comments(connector, config):
    issue = _make_issue("PROJ-2", "Comment test", comment_count=3)
    search_resp = _make_response({"issues": [issue], "total": 1, "startAt": 0})

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=search_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        docs = []
        async for doc, _ in connector.fetch_documents(config, None):
            docs.append(doc)

    assert len(docs) == 1
    # Comments should appear in content
    assert "Comment 0 body" in docs[0].content
    assert "Comment 2 body" in docs[0].content


@pytest.mark.asyncio
async def test_fetch_documents_pagination(connector, config):
    """Two pages of results should both be yielded."""
    issue_page1 = [_make_issue(f"PROJ-{i}") for i in range(100)]
    issue_page2 = [_make_issue("PROJ-100")]

    call_count = 0

    def mock_get_side_effect(url, *, params=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response({"issues": issue_page1, "total": 101, "startAt": 0})
        return _make_response({"issues": issue_page2, "total": 101, "startAt": 100})

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=mock_get_side_effect)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        docs = []
        async for doc, _ in connector.fetch_documents(config, None):
            docs.append(doc)

    assert len(docs) == 101


@pytest.mark.asyncio
async def test_fetch_documents_empty(connector, config):
    search_resp = _make_response({"issues": [], "total": 0, "startAt": 0})

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=search_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        docs = []
        async for doc, _ in connector.fetch_documents(config, None):
            docs.append(doc)

    assert docs == []


@pytest.mark.asyncio
async def test_fetch_documents_incremental(connector, config):
    """Cursor last_sync_at should be passed as JQL updated condition."""
    search_resp = _make_response({"issues": [], "total": 0, "startAt": 0})
    captured_params: list[dict] = []

    async def capture_get(url, *, params=None):
        captured_params.append(params or {})
        return search_resp

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=capture_get)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        async for _ in connector.fetch_documents(
            config, {"last_sync_at": "2024-06-01T00:00:00Z"}
        ):
            pass

    assert len(captured_params) == 1
    jql = captured_params[0].get("jql", "")
    assert "updated" in jql
    assert "2024-06-01" in jql


# ---------------------------------------------------------------------------
# handle_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_webhook_issue_created(connector):
    payload = {
        "webhookEvent": "jira:issue_created",
        "_domain": "acme.atlassian.net",
        "issue": _make_issue("PROJ-10", "New feature request"),
    }
    docs = await connector.handle_webhook(payload)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_type == "jira_issue"
    assert doc.source_id == "jira:acme.atlassian.net:PROJ-10"
    assert doc.title == "New feature request"


@pytest.mark.asyncio
async def test_handle_webhook_issue_updated(connector):
    payload = {
        "webhookEvent": "jira:issue_updated",
        "_domain": "acme.atlassian.net",
        "issue": _make_issue("PROJ-11", "Updated issue"),
    }
    docs = await connector.handle_webhook(payload)

    assert len(docs) == 1
    assert docs[0].source_type == "jira_issue"


@pytest.mark.asyncio
async def test_handle_webhook_unknown_event(connector):
    payload = {
        "webhookEvent": "jira:issue_deleted",
        "_domain": "acme.atlassian.net",
        "issue": _make_issue("PROJ-12"),
    }
    docs = await connector.handle_webhook(payload)
    assert docs == []


@pytest.mark.asyncio
async def test_handle_webhook_no_issue(connector):
    payload = {
        "webhookEvent": "jira:issue_created",
        "_domain": "acme.atlassian.net",
    }
    docs = await connector.handle_webhook(payload)
    assert docs == []


@pytest.mark.asyncio
async def test_handle_webhook_empty_payload(connector):
    docs = await connector.handle_webhook({})
    assert docs == []


# ---------------------------------------------------------------------------
# Document shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_shape_from_webhook(connector):
    payload = {
        "webhookEvent": "jira:issue_created",
        "_domain": "acme.atlassian.net",
        "issue": _make_issue("PROJ-5", "Shape test", comment_count=1),
    }
    docs = await connector.handle_webhook(payload)
    doc = docs[0]

    assert isinstance(doc.source_id, str)
    assert isinstance(doc.source_type, str)
    assert isinstance(doc.title, str)
    assert isinstance(doc.content, str)
    assert doc.url.startswith("https://")
    assert isinstance(doc.metadata, dict)
    assert "issue_key" in doc.metadata
    assert "project_key" in doc.metadata
    assert "status" in doc.metadata


# ---------------------------------------------------------------------------
# JQL builder
# ---------------------------------------------------------------------------


def test_jql_no_filters(connector):
    jql = connector._build_jql([], None)
    assert jql == "ORDER BY updated ASC"


def test_jql_project_filter(connector):
    jql = connector._build_jql(["PROJ", "CORE"], None)
    assert 'project in ("PROJ", "CORE")' in jql
    assert "ORDER BY updated ASC" in jql
    assert "AND ORDER BY" not in jql


def test_jql_date_filter(connector):
    jql = connector._build_jql([], "2024-01-15T12:30:00Z")
    assert 'updated >=' in jql
    assert "2024-01-15" in jql
    assert "ORDER BY updated ASC" in jql
    assert "AND ORDER BY" not in jql


def test_jql_combined_filters(connector):
    jql = connector._build_jql(["PROJ"], "2024-06-01T00:00:00Z")
    assert "project" in jql
    assert "updated" in jql
    assert "ORDER BY updated ASC" in jql
    # Verify AND separates conditions but not ORDER BY
    assert "AND ORDER BY" not in jql
    assert " AND " in jql


def test_jql_invalid_date_gracefully_ignored(connector):
    # Should not raise, just log a warning
    jql = connector._build_jql([], "not-a-date")
    assert "ORDER BY updated ASC" in jql


# ---------------------------------------------------------------------------
# Domain URL handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_with_https_prefix(connector):
    config = {
        "email": "alice@example.com",
        "api_token": "token",
        "domain": "https://acme.atlassian.net",
    }
    search_resp = _make_response({"issues": [], "total": 0, "startAt": 0})

    with patch("aidomaincontext.connectors.jira.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=search_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        async for _ in connector.fetch_documents(config, None):
            pass

        # Should not double-prepend https://
        call_url = str(instance.get.call_args[0][0])
        assert "https://https://" not in call_url
