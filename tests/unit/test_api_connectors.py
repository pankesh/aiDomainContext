"""Unit tests for the connector-related API endpoints.

These tests mock the database dependency so no real DB connection is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aidomaincontext.main import app
from aidomaincontext.models.database import get_session


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_connector_orm(
    *,
    name: str = "My Slack",
    connector_type: str = "slack",
    enabled: bool = True,
    sync_cursor: dict | None = None,
) -> MagicMock:
    """Create a fake ORM Connector row."""
    obj = MagicMock()
    obj.id = uuid.uuid4()
    obj.name = name
    obj.connector_type = connector_type
    obj.config_encrypted = {"bot_token": "xoxb-redacted"}
    obj.sync_cursor = sync_cursor
    obj.enabled = enabled
    obj.created_at = _utcnow()
    obj.updated_at = _utcnow()
    return obj


def _make_document_orm(
    *,
    source_id: str = "slack:C001:ts1",
    source_type: str = "slack_message",
) -> MagicMock:
    """Create a fake ORM Document row."""
    obj = MagicMock()
    obj.id = uuid.uuid4()
    obj.connector_id = uuid.uuid4()
    obj.source_id = source_id
    obj.source_type = source_type
    obj.title = "Test doc"
    obj.content = "Content body"
    obj.url = "https://example.com"
    obj.author = "testuser"
    obj.metadata_ = {}
    obj.permissions = {"is_public": True}
    obj.content_hash = "abc123"
    obj.created_at = _utcnow()
    obj.updated_at = _utcnow()
    return obj


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def client(mock_session):
    """TestClient with the DB session dependency overridden."""

    async def override_get_session():
        yield mock_session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ #
# GET /api/v1/health
# ------------------------------------------------------------------ #


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ------------------------------------------------------------------ #
# GET /api/v1/stats
# ------------------------------------------------------------------ #


def test_stats(client, mock_session):
    mock_session.scalar = AsyncMock(side_effect=[42, 128])

    resp = client.get("/api/v1/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["documents"] == 42
    assert data["chunks"] == 128


# ------------------------------------------------------------------ #
# GET /api/v1/documents
# ------------------------------------------------------------------ #


def test_list_documents(client, mock_session):
    doc1 = _make_document_orm(source_id="slack:C001:ts1")
    doc2 = _make_document_orm(source_id="github:acme/repo:issue:1", source_type="github_issue")

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [doc1, doc2]
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.get("/api/v1/documents")
    assert resp.status_code == 200
    docs = resp.json()
    assert len(docs) == 2
    assert docs[0]["source_id"] == "slack:C001:ts1"
    assert docs[1]["source_type"] == "github_issue"


def test_list_documents_empty(client, mock_session):
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.get("/api/v1/documents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_documents_with_pagination(client, mock_session):
    doc = _make_document_orm()

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [doc]
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.get("/api/v1/documents?limit=1&offset=5")
    assert resp.status_code == 200
    docs = resp.json()
    assert len(docs) == 1


# ------------------------------------------------------------------ #
# Document response shape
# ------------------------------------------------------------------ #


def test_document_response_shape(client, mock_session):
    doc = _make_document_orm()

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [doc]
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.get("/api/v1/documents")
    assert resp.status_code == 200
    item = resp.json()[0]

    # Verify all expected keys are present
    expected_keys = {
        "id",
        "connector_id",
        "source_id",
        "source_type",
        "title",
        "content",
        "url",
        "author",
        "metadata",
        "permissions",
        "content_hash",
        "created_at",
        "updated_at",
    }
    assert expected_keys.issubset(set(item.keys()))
