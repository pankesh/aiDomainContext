"""Unit tests for the GitHub connector."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aidomaincontext.connectors.github import GitHubConnector
from tests.fixtures.github_responses import (
    COMMIT_DETAIL_RESPONSE,
    COMMITS_LIST_RESPONSE,
    GITHUB_ISSUES_EVENT,
    GITHUB_PR_EVENT,
    GITHUB_PUSH_EVENT,
    ISSUE_COMMENTS_RESPONSE,
    ISSUES_LIST_RESPONSE,
    PR_REVIEW_COMMENTS_RESPONSE,
    PULLS_LIST_RESPONSE,
)


@pytest.fixture
def connector():
    return GitHubConnector()


@pytest.fixture
def config():
    return {"access_token": "ghp_testtoken123", "repos": ["acme/webapp"]}


# ------------------------------------------------------------------ #
# connector_type
# ------------------------------------------------------------------ #


def test_connector_type(connector):
    assert connector.connector_type == "github"


# ------------------------------------------------------------------ #
# validate_credentials
# ------------------------------------------------------------------ #


def _make_response(
    json_data,
    status_code: int = 200,
    headers: dict | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        headers=headers or {},
        request=httpx.Request("GET", "https://api.github.com/user"),
    )


@pytest.mark.asyncio
async def test_validate_credentials_success(connector, config):
    mock_resp = _make_response({"login": "alice"})

    with patch("aidomaincontext.connectors.github.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is True


@pytest.mark.asyncio
async def test_validate_credentials_bad_token(connector):
    mock_resp = _make_response({"message": "Bad credentials"}, status_code=401)

    with patch("aidomaincontext.connectors.github.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials({"access_token": "ghp_bad"})
        assert result is False


@pytest.mark.asyncio
async def test_validate_credentials_network_error(connector, config):
    with patch("aidomaincontext.connectors.github.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await connector.validate_credentials(config)
        assert result is False


# ------------------------------------------------------------------ #
# fetch_documents
# ------------------------------------------------------------------ #


def _response(json_data, url: str = "https://api.github.com/test", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json=json_data, headers=headers or {}, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_fetch_documents_issues(connector, config):
    """Issues (excluding items with pull_request key) should produce github_issue docs."""

    async def mock_get(url, *, params=None, headers=None):
        url_str = str(url)
        if "/issues/" in url_str and "/comments" in url_str:
            return _response(ISSUE_COMMENTS_RESPONSE, url=url_str)
        if "/issues" in url_str:
            return _response(ISSUES_LIST_RESPONSE, url=url_str)
        if "/pulls/" in url_str and "/comments" in url_str:
            return _response(PR_REVIEW_COMMENTS_RESPONSE, url=url_str)
        if "/pulls" in url_str:
            return _response(PULLS_LIST_RESPONSE, url=url_str)
        if "/commits/" in url_str:
            return _response(COMMIT_DETAIL_RESPONSE, url=url_str)
        if "/commits" in url_str:
            return _response(COMMITS_LIST_RESPONSE, url=url_str)
        raise AssertionError(f"Unexpected URL: {url_str}")

    with patch("aidomaincontext.connectors.github.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=mock_get)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        docs = []
        async for doc, cursor in connector.fetch_documents(config, None):
            docs.append(doc)

        # Expected: 1 issue (item with pull_request key skipped) +
        #           2 PRs + 2 commits = 5
        assert len(docs) == 5

        issues = [d for d in docs if d.source_type == "github_issue"]
        prs = [d for d in docs if d.source_type == "github_pr"]
        commits = [d for d in docs if d.source_type == "github_commit"]

        assert len(issues) == 1
        assert len(prs) == 2
        assert len(commits) == 2

        # Verify issue shape
        issue = issues[0]
        assert issue.source_id == "github:acme/webapp:issue:42"
        assert issue.author == "alice"
        assert issue.metadata["repo"] == "acme/webapp"
        assert issue.metadata["state"] == "open"
        assert "bug" in issue.metadata["labels"]
        # Comments should be included in content
        assert "WebKit regression" in issue.content

        # Verify PR shape
        pr0 = prs[0]
        assert pr0.source_id == "github:acme/webapp:pr:50"
        assert pr0.source_type == "github_pr"
        assert pr0.metadata["merged"] is True

        pr1 = prs[1]
        assert pr1.metadata["merged"] is False

        # Verify commit shape
        commit0 = commits[0]
        assert commit0.source_id == "github:acme/webapp:commit:abc123def456"
        assert commit0.source_type == "github_commit"
        assert "refresh tokens" in commit0.title


@pytest.mark.asyncio
async def test_fetch_documents_incremental(connector, config):
    """With a cursor, since parameter should be passed along."""
    cursor_input = {"last_sync_at": "2024-02-01T00:00:00Z"}

    async def mock_get(url, *, params=None, headers=None):
        # Return empty lists to keep the test fast
        return _response([], url=str(url))

    with patch("aidomaincontext.connectors.github.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=mock_get)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        docs = []
        async for doc, cursor in connector.fetch_documents(config, cursor_input):
            docs.append(doc)

        assert len(docs) == 0  # empty responses -> no docs


@pytest.mark.asyncio
async def test_fetch_documents_cursor_output(connector, config):
    """The cursor yielded should contain last_sync_at with an ISO timestamp."""

    async def mock_get(url, *, params=None, headers=None):
        url_str = str(url)
        if "/issues" in url_str:
            # Return one simple issue
            return _response(
                [
                    {
                        "number": 1,
                        "title": "Test issue",
                        "body": "Body",
                        "state": "open",
                        "html_url": "https://github.com/acme/webapp/issues/1",
                        "comments": 0,
                        "comments_url": "https://api.github.com/repos/acme/webapp/issues/1/comments",
                        "user": {"login": "dev"},
                        "labels": [],
                        "updated_at": "2024-03-01T00:00:00Z",
                    }
                ],
                url=url_str,
            )
        return _response([], url=url_str)

    with patch("aidomaincontext.connectors.github.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=mock_get)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        cursors = []
        async for doc, cursor in connector.fetch_documents(config, None):
            cursors.append(cursor)

        assert len(cursors) >= 1
        assert "last_sync_at" in cursors[0]
        # Should be an ISO-formatted timestamp string
        assert "T" in cursors[0]["last_sync_at"]


# ------------------------------------------------------------------ #
# handle_webhook — push
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_handle_webhook_push(connector):
    docs = await connector.handle_webhook(GITHUB_PUSH_EVENT)

    assert len(docs) == 2
    for doc in docs:
        assert doc.source_type == "github_commit"
        assert doc.metadata["repo"] == "acme/webapp"

    assert docs[0].source_id == "github:acme/webapp:commit:aaa111bbb222"
    assert "user avatars" in docs[0].title
    assert docs[0].author == "alice"
    # File changes should appear in content
    assert "src/avatars.py" in docs[0].content

    assert docs[1].source_id == "github:acme/webapp:commit:ccc333ddd444"


# ------------------------------------------------------------------ #
# handle_webhook — issues
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_handle_webhook_issues(connector):
    docs = await connector.handle_webhook(GITHUB_ISSUES_EVENT)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_type == "github_issue"
    assert doc.source_id == "github:acme/webapp:issue:99"
    assert doc.title == "Feature request: export to CSV"
    assert doc.author == "grace"
    assert doc.metadata["state"] == "open"
    assert "enhancement" in doc.metadata["labels"]


# ------------------------------------------------------------------ #
# handle_webhook — pull_request
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_handle_webhook_pull_request(connector):
    docs = await connector.handle_webhook(GITHUB_PR_EVENT)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_type == "github_pr"
    assert doc.source_id == "github:acme/webapp:pr:60"
    assert doc.title == "Add CSV export endpoint"
    assert doc.metadata["merged"] is False


# ------------------------------------------------------------------ #
# handle_webhook — unknown event
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_handle_webhook_unknown_event(connector):
    docs = await connector.handle_webhook({"event_type": "deployment", "body": {}})
    assert docs == []


@pytest.mark.asyncio
async def test_handle_webhook_empty_payload(connector):
    docs = await connector.handle_webhook({})
    assert docs == []


# ------------------------------------------------------------------ #
# handle_webhook — issues event with missing issue key
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_handle_webhook_issues_no_issue(connector):
    payload = {"event_type": "issues", "body": {"action": "opened"}}
    docs = await connector.handle_webhook(payload)
    assert docs == []


# ------------------------------------------------------------------ #
# Document shape validation
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_document_shape_from_webhook(connector):
    """All DocumentBase fields should be properly set."""
    docs = await connector.handle_webhook(GITHUB_ISSUES_EVENT)
    doc = docs[0]

    assert isinstance(doc.source_id, str)
    assert isinstance(doc.source_type, str)
    assert isinstance(doc.title, str)
    assert isinstance(doc.content, str)
    assert doc.url is not None
    assert isinstance(doc.metadata, dict)
    assert "repo" in doc.metadata
    assert "number" in doc.metadata
