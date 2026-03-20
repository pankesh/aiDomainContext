"""Unit tests for routes_search.py and routes_upload.py.

All external dependencies (Redis, DB session, hybrid_search, generate_answer,
ingest_document) are mocked so no real infrastructure is needed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aidomaincontext.main import app
from aidomaincontext.models.database import get_session
from aidomaincontext.schemas.search import Citation, Message


# ------------------------------------------------------------------ #
# Shared helpers
# ------------------------------------------------------------------ #


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_chunk(document_id: uuid.UUID | None = None) -> dict:
    return {
        "id": uuid.uuid4(),
        "document_id": document_id or uuid.uuid4(),
        "chunk_index": 0,
        "content": "sample chunk content",
        "token_count": 10,
        "score": 0.85,
    }


def _make_doc_orm(*, document_id: uuid.UUID | None = None, title: str = "Doc Title", url: str = "https://example.com") -> MagicMock:
    obj = MagicMock()
    obj.id = document_id or uuid.uuid4()
    obj.connector_id = uuid.uuid4()
    obj.source_id = "file:test.txt"
    obj.source_type = "file_upload"
    obj.title = title
    obj.url = url
    obj.content = "doc content body"
    obj.author = None
    obj.metadata_ = {}
    obj.permissions = {"is_public": True}
    obj.content_hash = "deadbeef"
    obj.created_at = _utcnow()
    obj.updated_at = _utcnow()
    return obj


# ------------------------------------------------------------------ #
# DB session fixture (shared by search + upload route tests)
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
    """TestClient with DB session dependency overridden."""

    async def override_get_session():
        yield mock_session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


# ================================================================== #
# Module 1 — routes_search.py                                        #
# ================================================================== #


# ------------------------------------------------------------------ #
# Helper: _load_session_history
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_load_session_history_returns_empty_list_when_redis_has_no_key():
    from aidomaincontext.api.routes_search import _load_session_history

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    result = await _load_session_history(mock_redis, "non-existent-session")

    assert result == []
    mock_redis.get.assert_awaited_once_with("chat:session:non-existent-session")


@pytest.mark.asyncio
async def test_load_session_history_deserialises_stored_json():
    from aidomaincontext.api.routes_search import _load_session_history

    stored = json.dumps([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ])
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=stored)

    result = await _load_session_history(mock_redis, "abc-session")

    assert len(result) == 2
    assert result[0] == Message(role="user", content="hello")
    assert result[1] == Message(role="assistant", content="hi there")


# ------------------------------------------------------------------ #
# Helper: _save_session_history
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_save_session_history_calls_setex_with_correct_args():
    from aidomaincontext.api.routes_search import _save_session_history
    from aidomaincontext.config import settings

    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock()

    session_id = "my-session-id"
    history = [
        Message(role="user", content="question"),
        Message(role="assistant", content="answer"),
    ]

    await _save_session_history(mock_redis, session_id, history)

    expected_key = f"chat:session:{session_id}"
    expected_ttl = settings.chat_session_ttl_seconds
    expected_payload = json.dumps([m.model_dump() for m in history])

    mock_redis.setex.assert_awaited_once_with(expected_key, expected_ttl, expected_payload)


# ------------------------------------------------------------------ #
# Helper: _enrich_chunks_with_doc_info
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_enrich_chunks_returns_empty_list_unchanged():
    from aidomaincontext.api.routes_search import _enrich_chunks_with_doc_info

    mock_session = AsyncMock()
    result = await _enrich_chunks_with_doc_info(mock_session, [])

    assert result == []
    mock_session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_chunks_populates_title_and_url():
    from aidomaincontext.api.routes_search import _enrich_chunks_with_doc_info

    doc_id = uuid.uuid4()
    chunk = _make_chunk(document_id=doc_id)
    # Remove pre-existing title/url if present
    chunk.pop("title", None)
    chunk.pop("url", None)

    doc = _make_doc_orm(document_id=doc_id, title="My Doc", url="https://docs.example.com")

    mock_scalars = MagicMock()
    mock_scalars.__iter__ = MagicMock(return_value=iter([doc]))
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await _enrich_chunks_with_doc_info(mock_session, [chunk])

    assert result[0]["title"] == "My Doc"
    assert result[0]["url"] == "https://docs.example.com"


@pytest.mark.asyncio
async def test_enrich_chunks_handles_missing_doc_gracefully():
    """Chunk whose document_id has no matching DB row must be returned as-is."""
    from aidomaincontext.api.routes_search import _enrich_chunks_with_doc_info

    chunk = _make_chunk()

    # DB returns no rows
    mock_scalars = MagicMock()
    mock_scalars.__iter__ = MagicMock(return_value=iter([]))
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await _enrich_chunks_with_doc_info(mock_session, [chunk])

    assert result == [chunk]
    assert "title" not in result[0]


# ------------------------------------------------------------------ #
# POST /api/v1/search
# ------------------------------------------------------------------ #


def test_post_search_returns_200_with_chunks(client, mock_session):
    chunk = _make_chunk()

    # hybrid_search returns a list of chunk dicts
    with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[chunk])):
        # _enrich_chunks_with_doc_info is a coroutine in the same module; mock it too
        with patch(
            "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
            new=AsyncMock(return_value=[chunk]),
        ):
            resp = client.post("/api/v1/search", json={"query": "test query", "top_k": 3})

    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "test query"
    assert len(data["chunks"]) == 1
    assert data["chunks"][0]["content"] == "sample chunk content"
    assert data["chunks"][0]["score"] == pytest.approx(0.85)


def test_post_search_returns_empty_chunks_when_none_found(client, mock_session):
    with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[])):
        with patch(
            "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.post("/api/v1/search", json={"query": "nothing"})

    assert resp.status_code == 200
    assert resp.json()["chunks"] == []


def test_post_search_missing_query_returns_422(client):
    resp = client.post("/api/v1/search", json={})
    assert resp.status_code == 422


# ------------------------------------------------------------------ #
# POST /api/v1/chat
# ------------------------------------------------------------------ #


def _make_mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.aclose = AsyncMock()
    return redis


def test_post_chat_new_session_generates_uuid_and_returns_it(client, mock_session):
    chunk = _make_chunk()
    mock_redis = _make_mock_redis()
    citations = [Citation(document_title="Doc", document_url=None, chunk_content="text")]

    with patch("aidomaincontext.api.routes_search._get_redis", new=AsyncMock(return_value=mock_redis)):
        with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[chunk])):
            with patch(
                "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
                new=AsyncMock(return_value=[chunk]),
            ):
                with patch(
                    "aidomaincontext.api.routes_search.generate_answer",
                    new=AsyncMock(return_value=("The answer.", citations)),
                ):
                    resp = client.post("/api/v1/chat", json={"query": "What is RAG?"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "The answer."
    assert data["query"] == "What is RAG?"
    # session_id must be a valid UUID string
    returned_sid = data["session_id"]
    uuid.UUID(returned_sid)  # raises ValueError if not a UUID


def test_post_chat_existing_session_id_is_echoed_back(client, mock_session):
    existing_sid = str(uuid.uuid4())
    chunk = _make_chunk()
    mock_redis = _make_mock_redis()
    citations: list[Citation] = []

    with patch("aidomaincontext.api.routes_search._get_redis", new=AsyncMock(return_value=mock_redis)):
        with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[chunk])):
            with patch(
                "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
                new=AsyncMock(return_value=[chunk]),
            ):
                with patch(
                    "aidomaincontext.api.routes_search.generate_answer",
                    new=AsyncMock(return_value=("answer", citations)),
                ):
                    resp = client.post(
                        "/api/v1/chat",
                        json={"query": "follow-up", "session_id": existing_sid},
                    )

    assert resp.status_code == 200
    assert resp.json()["session_id"] == existing_sid


def test_post_chat_existing_session_loads_history_and_passes_to_generate_answer(client, mock_session):
    """History stored in Redis must be forwarded to generate_answer."""
    existing_sid = str(uuid.uuid4())
    stored_history = json.dumps([
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ])

    mock_redis = _make_mock_redis()
    mock_redis.get = AsyncMock(return_value=stored_history)

    chunk = _make_chunk()
    citations: list[Citation] = []
    generate_mock = AsyncMock(return_value=("new answer", citations))

    with patch("aidomaincontext.api.routes_search._get_redis", new=AsyncMock(return_value=mock_redis)):
        with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[chunk])):
            with patch(
                "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
                new=AsyncMock(return_value=[chunk]),
            ):
                with patch("aidomaincontext.api.routes_search.generate_answer", new=generate_mock):
                    resp = client.post(
                        "/api/v1/chat",
                        json={"query": "second question", "session_id": existing_sid},
                    )

    assert resp.status_code == 200
    # Verify generate_answer received the two-turn history
    _, call_kwargs = generate_mock.call_args
    passed_history = call_kwargs.get("history") or generate_mock.call_args.args[2]
    assert len(passed_history) == 2
    assert passed_history[0].role == "user"
    assert passed_history[0].content == "first question"


def test_post_chat_invalid_session_id_returns_422(client, mock_session):
    resp = client.post("/api/v1/chat", json={"query": "hi", "session_id": "not-a-uuid"})
    assert resp.status_code == 422


def test_post_chat_redis_aclose_called_in_finally(client, mock_session):
    """redis.aclose() must be called even when generate_answer raises.

    TestClient re-raises unhandled server exceptions, so we catch the
    RuntimeError with pytest.raises and assert on aclose() afterwards.
    """
    mock_redis = _make_mock_redis()

    with patch("aidomaincontext.api.routes_search._get_redis", new=AsyncMock(return_value=mock_redis)):
        with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[])):
            with patch(
                "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
                new=AsyncMock(return_value=[]),
            ):
                with patch(
                    "aidomaincontext.api.routes_search.generate_answer",
                    new=AsyncMock(side_effect=RuntimeError("LLM error")),
                ):
                    with pytest.raises(RuntimeError, match="LLM error"):
                        client.post("/api/v1/chat", json={"query": "crash me"})

    # Despite the exception the finally block must have closed Redis
    mock_redis.aclose.assert_awaited_once()


def test_post_chat_saves_updated_history_to_redis(client, mock_session):
    """After a successful turn, history must be persisted via setex."""
    sid = str(uuid.uuid4())
    mock_redis = _make_mock_redis()
    chunk = _make_chunk()
    citations: list[Citation] = []

    with patch("aidomaincontext.api.routes_search._get_redis", new=AsyncMock(return_value=mock_redis)):
        with patch("aidomaincontext.api.routes_search.hybrid_search", new=AsyncMock(return_value=[chunk])):
            with patch(
                "aidomaincontext.api.routes_search._enrich_chunks_with_doc_info",
                new=AsyncMock(return_value=[chunk]),
            ):
                with patch(
                    "aidomaincontext.api.routes_search.generate_answer",
                    new=AsyncMock(return_value=("my answer", citations)),
                ):
                    resp = client.post(
                        "/api/v1/chat",
                        json={"query": "store me", "session_id": sid},
                    )

    assert resp.status_code == 200
    mock_redis.setex.assert_awaited_once()
    # The key passed to setex must contain the session id
    key_arg = mock_redis.setex.call_args.args[0]
    assert sid in key_arg


# ================================================================== #
# Module 2 — routes_upload.py                                        #
# ================================================================== #


def _make_ingest_doc_return(title: str = "uploaded.txt") -> MagicMock:
    """Fake ORM Document returned by ingest_document."""
    doc = MagicMock()
    doc.id = uuid.uuid4()
    doc.connector_id = None
    doc.source_id = f"file:{title}"
    doc.source_type = "file_upload"
    doc.title = title
    doc.content = "file content body"
    doc.url = None
    doc.author = None
    doc.metadata_ = {}
    doc.permissions = {"is_public": True}
    doc.content_hash = "cafebabe"
    doc.created_at = _utcnow()
    doc.updated_at = _utcnow()
    return doc


# ------------------------------------------------------------------ #
# Text file via content-type
# ------------------------------------------------------------------ #


def test_upload_text_plain_calls_ingest_without_file_path(client, mock_session):
    fake_doc = _make_ingest_doc_return("hello.txt")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ) as mock_ingest:
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("hello.txt", b"hello world", "text/plain")},
        )

    assert resp.status_code == 200
    # file_path kwarg must NOT be present — text path uses raw content
    call_kwargs = mock_ingest.call_args.kwargs
    assert "file_path" not in call_kwargs

    data = resp.json()
    assert data["title"] == "hello.txt"
    assert data["source_type"] == "file_upload"


def test_upload_text_plain_response_shape(client, mock_session):
    fake_doc = _make_ingest_doc_return("notes.txt")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ):
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("notes.txt", b"some notes", "text/plain")},
        )

    assert resp.status_code == 200
    keys = set(resp.json().keys())
    expected = {"id", "connector_id", "source_id", "source_type", "title", "content",
                "url", "author", "metadata", "permissions", "content_hash", "created_at", "updated_at"}
    assert expected.issubset(keys)


# ------------------------------------------------------------------ #
# Text file detected by extension (.md)
# ------------------------------------------------------------------ #


def test_upload_md_extension_detected_as_text(client, mock_session):
    """A .md file uploaded with an opaque content-type must still take the text path."""
    fake_doc = _make_ingest_doc_return("README.md")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ) as mock_ingest:
        resp = client.post(
            "/api/v1/upload",
            # content-type is generic but filename ends with .md
            files={"file": ("README.md", b"# Hello", "application/octet-stream")},
        )

    assert resp.status_code == 200
    call_kwargs = mock_ingest.call_args.kwargs
    assert "file_path" not in call_kwargs


def test_upload_json_extension_detected_as_text(client, mock_session):
    fake_doc = _make_ingest_doc_return("data.json")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ) as mock_ingest:
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("data.json", b'{"key": "val"}', "application/octet-stream")},
        )

    assert resp.status_code == 200
    call_kwargs = mock_ingest.call_args.kwargs
    assert "file_path" not in call_kwargs


# ------------------------------------------------------------------ #
# Binary file (PDF) — tempfile path
# ------------------------------------------------------------------ #


def test_upload_pdf_calls_ingest_with_file_path(client, mock_session):
    fake_doc = _make_ingest_doc_return("report.pdf")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ) as mock_ingest:
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("report.pdf", b"%PDF-1.4 binary content", "application/pdf")},
        )

    assert resp.status_code == 200
    # Binary path must pass file_path kwarg to ingest_document
    call_kwargs = mock_ingest.call_args.kwargs
    assert "file_path" in call_kwargs
    assert call_kwargs["file_path"].endswith(".pdf")


def test_upload_pdf_response_200(client, mock_session):
    fake_doc = _make_ingest_doc_return("report.pdf")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ):
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("report.pdf", b"%PDF binary", "application/pdf")},
        )

    assert resp.status_code == 200
    assert resp.json()["source_type"] == "file_upload"


def test_upload_binary_without_extension_uses_tempfile(client, mock_session):
    """Binary file with no recognisable extension must still use the tempfile path."""
    fake_doc = _make_ingest_doc_return("binaryblob")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ) as mock_ingest:
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("binaryblob", b"\x00\x01\x02\x03", "application/octet-stream")},
        )

    assert resp.status_code == 200
    call_kwargs = mock_ingest.call_args.kwargs
    assert "file_path" in call_kwargs


# ------------------------------------------------------------------ #
# Edge cases
# ------------------------------------------------------------------ #


def test_upload_markdown_content_type_directly(client, mock_session):
    """text/markdown content-type must also take the text path."""
    fake_doc = _make_ingest_doc_return("doc.md")

    with patch(
        "aidomaincontext.api.routes_upload.ingest_document",
        new=AsyncMock(return_value=fake_doc),
    ) as mock_ingest:
        resp = client.post(
            "/api/v1/upload",
            files={"file": ("doc.md", b"# Title\nBody text", "text/markdown")},
        )

    assert resp.status_code == 200
    call_kwargs = mock_ingest.call_args.kwargs
    assert "file_path" not in call_kwargs


def test_upload_missing_file_returns_422(client):
    resp = client.post("/api/v1/upload")
    assert resp.status_code == 422
