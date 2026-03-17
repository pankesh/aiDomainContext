"""Unit tests for the sync worker (run_sync_job)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aidomaincontext.schemas.documents import DocumentBase


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _make_connector_row(
    connector_id: uuid.UUID | None = None,
    connector_type: str = "slack",
    config: dict | None = None,
    sync_cursor: dict | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = connector_id or uuid.uuid4()
    row.connector_type = connector_type
    row.config_encrypted = config or {"bot_token": "xoxb-test"}
    row.sync_cursor = sync_cursor
    row.enabled = True
    return row


def _make_document(source_id: str = "slack:C001:ts1") -> DocumentBase:
    return DocumentBase(
        source_id=source_id,
        source_type="slack_message",
        title="Test message",
        content="Hello world",
    )


async def _fake_fetch_documents(config, cursor):
    """An async generator that yields two documents."""
    doc1 = _make_document("slack:C001:ts1")
    doc2 = _make_document("slack:C001:ts2")
    yield doc1, {"last_sync_ts": "100"}
    yield doc2, {"last_sync_ts": "200"}


async def _fake_fetch_documents_with_failure(config, cursor):
    """An async generator where the second doc causes an ingestion failure."""
    yield _make_document("slack:C001:ts1"), {"last_sync_ts": "100"}
    yield _make_document("slack:C001:ts2"), {"last_sync_ts": "200"}


async def _fake_fetch_empty(config, cursor):
    """An async generator that yields nothing."""
    return
    yield  # noqa: RET504  — makes this a generator


async def _fake_fetch_exploding(config, cursor):
    """An async generator that raises immediately."""
    raise RuntimeError("Upstream API is down")
    yield  # noqa: RET504  — unreachable, but needed for generator syntax


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_run_sync_job_success():
    """Happy path: two documents synced, job status becomes 'completed'."""
    connector_id = uuid.uuid4()
    connector_row = _make_connector_row(connector_id=connector_id)

    mock_impl = MagicMock()
    mock_impl.fetch_documents = _fake_fetch_documents

    # Mock the async session context manager
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = connector_row
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aidomaincontext.sync.worker.async_session", return_value=mock_session_cm),
        patch("aidomaincontext.sync.worker.get_connector", return_value=mock_impl),
        patch("aidomaincontext.sync.worker.ingest_document", new_callable=AsyncMock) as mock_ingest,
    ):
        from aidomaincontext.sync.worker import run_sync_job

        job = await run_sync_job(connector_id)

    assert job.status == "completed"
    assert job.documents_synced == 2
    assert job.documents_failed == 0
    assert job.finished_at is not None
    assert mock_ingest.await_count == 2
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_sync_job_connector_not_found():
    """When the connector row doesn't exist, a ValueError should be raised."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aidomaincontext.sync.worker.async_session", return_value=mock_session_cm):
        from aidomaincontext.sync.worker import run_sync_job

        with pytest.raises(ValueError, match="not found"):
            await run_sync_job(uuid.uuid4())


@pytest.mark.asyncio
async def test_run_sync_job_partial_failure():
    """When ingestion fails for one document, it should still count as failed and continue."""
    connector_id = uuid.uuid4()
    connector_row = _make_connector_row(connector_id=connector_id)

    mock_impl = MagicMock()
    mock_impl.fetch_documents = _fake_fetch_documents_with_failure

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = connector_row
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    call_count = 0

    async def ingest_side_effect(session, doc_data, connector_id):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Embedding API timeout")

    with (
        patch("aidomaincontext.sync.worker.async_session", return_value=mock_session_cm),
        patch("aidomaincontext.sync.worker.get_connector", return_value=mock_impl),
        patch(
            "aidomaincontext.sync.worker.ingest_document",
            new_callable=AsyncMock,
            side_effect=ingest_side_effect,
        ),
    ):
        from aidomaincontext.sync.worker import run_sync_job

        job = await run_sync_job(connector_id)

    # The job should still complete, but with one failure
    assert job.status == "completed"
    assert job.documents_synced == 1
    assert job.documents_failed == 1


@pytest.mark.asyncio
async def test_run_sync_job_connector_error():
    """When the connector's fetch_documents itself raises, job status should be 'failed'."""
    connector_id = uuid.uuid4()
    connector_row = _make_connector_row(connector_id=connector_id)

    mock_impl = MagicMock()
    mock_impl.fetch_documents = _fake_fetch_exploding

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = connector_row
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aidomaincontext.sync.worker.async_session", return_value=mock_session_cm),
        patch("aidomaincontext.sync.worker.get_connector", return_value=mock_impl),
        patch("aidomaincontext.sync.worker.ingest_document", new_callable=AsyncMock),
    ):
        from aidomaincontext.sync.worker import run_sync_job

        job = await run_sync_job(connector_id)

    assert job.status == "failed"
    assert "Upstream API is down" in job.error_message
    assert job.finished_at is not None
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_sync_job_empty_sync():
    """When the connector yields no documents, job should complete with 0 counts."""
    connector_id = uuid.uuid4()
    connector_row = _make_connector_row(connector_id=connector_id)

    mock_impl = MagicMock()
    mock_impl.fetch_documents = _fake_fetch_empty

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = connector_row
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aidomaincontext.sync.worker.async_session", return_value=mock_session_cm),
        patch("aidomaincontext.sync.worker.get_connector", return_value=mock_impl),
        patch("aidomaincontext.sync.worker.ingest_document", new_callable=AsyncMock) as mock_ingest,
    ):
        from aidomaincontext.sync.worker import run_sync_job

        job = await run_sync_job(connector_id)

    assert job.status == "completed"
    assert job.documents_synced == 0
    assert job.documents_failed == 0
    mock_ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_sync_job_full_sync_ignores_cursor():
    """sync_type='full' should pass cursor=None even if the connector has a stored cursor."""
    connector_id = uuid.uuid4()
    connector_row = _make_connector_row(
        connector_id=connector_id,
        sync_cursor={"last_sync_ts": "999"},
    )

    received_cursors = []

    async def capture_fetch(config, cursor):
        received_cursors.append(cursor)
        return
        yield  # async generator

    mock_impl = MagicMock()
    mock_impl.fetch_documents = capture_fetch

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = connector_row
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("aidomaincontext.sync.worker.async_session", return_value=mock_session_cm),
        patch("aidomaincontext.sync.worker.get_connector", return_value=mock_impl),
        patch("aidomaincontext.sync.worker.ingest_document", new_callable=AsyncMock),
    ):
        from aidomaincontext.sync.worker import run_sync_job

        job = await run_sync_job(connector_id, sync_type="full")

    assert received_cursors == [None]
    assert job.status == "completed"
