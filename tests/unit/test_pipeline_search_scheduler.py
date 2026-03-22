"""Unit tests for:
  - aidomaincontext.ingestion.pipeline  (ingest_document)
  - aidomaincontext.retrieval.hybrid_search (vector_search, bm25_search, hybrid_search)
  - aidomaincontext.sync.arq_worker (sync_connector_task, WorkerSettings)
  - aidomaincontext.sync.scheduler (_enqueue_sync, _enqueue_all_enabled_connectors,
                                     _enqueue_stale_connectors, start_scheduler,
                                     stop_scheduler)
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aidomaincontext.schemas.documents import DocumentBase


# ============================================================
# Helpers shared across test sections
# ============================================================


def _make_doc_data(
    source_id: str = "src:001",
    source_type: str = "file_upload",
    title: str = "Test Doc",
    content: str = "Hello world",
) -> DocumentBase:
    return DocumentBase(
        source_id=source_id,
        source_type=source_type,
        title=title,
        content=content,
    )


def _make_chunk_dict(chunk_id: uuid.UUID | None = None, score: float = 0.5) -> dict:
    return {
        "id": chunk_id or uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "chunk_index": 0,
        "content": "sample chunk content",
        "token_count": 10,
        "score": score,
    }


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ============================================================
# Module 1 — ingestion.pipeline.ingest_document
# ============================================================

PIPELINE_PATCHES = dict(
    extract_text="aidomaincontext.ingestion.pipeline.extract_text",
    chunk_text="aidomaincontext.ingestion.pipeline.chunk_text",
    embed_texts="aidomaincontext.ingestion.pipeline.embed_texts",
)

_CONTENT = "Hello world"
_CHUNKS = [
    {"chunk_index": 0, "content": "Hello world", "token_count": 2},
]
_EMBEDDINGS = [[0.1, 0.2, 0.3]]


def _make_async_session() -> AsyncMock:
    """Return a fully mocked AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    return session


def _mock_no_existing_doc(session: AsyncMock) -> None:
    """Configure session.execute to return no existing Document."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)


def _mock_existing_doc(session: AsyncMock, same_hash: bool = True) -> MagicMock:
    """Configure session.execute to return an existing Document mock."""
    content_hash = _content_hash(_CONTENT)
    doc = MagicMock()
    doc.content_hash = content_hash if same_hash else "old_hash_aabbcc"
    doc.chunks = [MagicMock(), MagicMock()]  # two old chunks

    result = MagicMock()
    result.scalar_one_or_none.return_value = doc
    session.execute = AsyncMock(return_value=result)
    return doc


@pytest.mark.asyncio
async def test_ingest_document_new_doc_created_and_committed():
    """New document: Document created, session.add called, session.commit called."""
    from aidomaincontext.ingestion.pipeline import ingest_document

    session = _make_async_session()
    _mock_no_existing_doc(session)
    doc_data = _make_doc_data()

    with (
        patch(PIPELINE_PATCHES["extract_text"], return_value=_CONTENT) as mock_extract,
        patch(PIPELINE_PATCHES["chunk_text"], return_value=_CHUNKS) as mock_chunk,
        patch(PIPELINE_PATCHES["embed_texts"], new_callable=AsyncMock, return_value=_EMBEDDINGS) as mock_embed,
    ):
        result = await ingest_document(session, doc_data)

    # extract_text called with correct kwargs
    mock_extract.assert_called_once_with(file_path=None, raw_text=_CONTENT)

    # chunk_text called on the extracted content
    mock_chunk.assert_called_once_with(_CONTENT)

    # embed_texts called once (one batch of 1 chunk)
    mock_embed.assert_awaited_once_with([_CHUNKS[0]["content"]])

    # session methods called
    session.add.assert_called()       # Document added
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()

    assert result is not None


@pytest.mark.asyncio
async def test_ingest_document_unchanged_returns_early():
    """Existing document with same content hash: returns early, no re-ingest."""
    from aidomaincontext.ingestion.pipeline import ingest_document

    session = _make_async_session()
    existing_doc = _mock_existing_doc(session, same_hash=True)
    doc_data = _make_doc_data()

    with (
        patch(PIPELINE_PATCHES["extract_text"], return_value=_CONTENT),
        patch(PIPELINE_PATCHES["chunk_text"]) as mock_chunk,
        patch(PIPELINE_PATCHES["embed_texts"], new_callable=AsyncMock) as mock_embed,
    ):
        result = await ingest_document(session, doc_data)

    # Pipeline short-circuits: no chunking, no embedding, no flush/commit
    mock_chunk.assert_not_called()
    mock_embed.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()

    # Returns the existing document unchanged
    assert result is existing_doc


@pytest.mark.asyncio
async def test_ingest_document_changed_hash_deletes_old_chunks_and_reingest():
    """Existing document with different hash: old chunks deleted, fields updated, re-ingested."""
    from aidomaincontext.ingestion.pipeline import ingest_document

    session = _make_async_session()
    existing_doc = _mock_existing_doc(session, same_hash=False)
    old_chunks = existing_doc.chunks  # two MagicMock chunks
    doc_data = _make_doc_data()

    with (
        patch(PIPELINE_PATCHES["extract_text"], return_value=_CONTENT),
        patch(PIPELINE_PATCHES["chunk_text"], return_value=_CHUNKS),
        patch(PIPELINE_PATCHES["embed_texts"], new_callable=AsyncMock, return_value=_EMBEDDINGS),
    ):
        result = await ingest_document(session, doc_data)

    # Every old chunk must be deleted
    assert session.delete.await_count == len(old_chunks)
    for old_chunk in old_chunks:
        session.delete.assert_any_await(old_chunk)

    # Document fields updated
    assert existing_doc.content == _CONTENT
    assert existing_doc.content_hash == _content_hash(_CONTENT)
    assert existing_doc.title == doc_data.title

    # Full pipeline continues: flush, embed, commit
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()

    assert result is existing_doc


@pytest.mark.asyncio
async def test_ingest_document_embedding_batched_for_large_chunk_count():
    """With >64 chunks, embed_texts is called in multiple batches of 64."""
    from aidomaincontext.ingestion.pipeline import EMBED_BATCH_SIZE, ingest_document

    num_chunks = 130  # spans 3 batches (64 + 64 + 2)
    chunks_data = [
        {"chunk_index": i, "content": f"chunk {i}", "token_count": 3}
        for i in range(num_chunks)
    ]
    embeddings_per_call = [[0.0] * 3 for _ in range(EMBED_BATCH_SIZE)]
    last_batch_size = num_chunks % EMBED_BATCH_SIZE or EMBED_BATCH_SIZE
    embeddings_last = [[0.0] * 3 for _ in range(last_batch_size)]

    session = _make_async_session()
    _mock_no_existing_doc(session)
    doc_data = _make_doc_data()

    embed_side_effects = [
        embeddings_per_call,  # batch 1 — 64 chunks
        embeddings_per_call,  # batch 2 — 64 chunks
        embeddings_last,      # batch 3 — 2 chunks
    ]

    with (
        patch(PIPELINE_PATCHES["extract_text"], return_value=_CONTENT),
        patch(PIPELINE_PATCHES["chunk_text"], return_value=chunks_data),
        patch(
            PIPELINE_PATCHES["embed_texts"],
            new_callable=AsyncMock,
            side_effect=embed_side_effects,
        ) as mock_embed,
    ):
        await ingest_document(session, doc_data)

    expected_calls = (num_chunks + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    assert mock_embed.await_count == expected_calls

    # Verify batch sizes from call args
    call_args_list = mock_embed.await_args_list
    assert len(call_args_list[0][0][0]) == EMBED_BATCH_SIZE
    assert len(call_args_list[1][0][0]) == EMBED_BATCH_SIZE
    assert len(call_args_list[2][0][0]) == num_chunks - 2 * EMBED_BATCH_SIZE


@pytest.mark.asyncio
async def test_ingest_document_passes_connector_id():
    """connector_id is threaded through correctly when creating a new Document."""
    from aidomaincontext.ingestion.pipeline import ingest_document

    session = _make_async_session()
    _mock_no_existing_doc(session)
    doc_data = _make_doc_data()
    connector_id = uuid.uuid4()

    created_docs = []

    original_add = session.add

    def capture_add(obj):
        created_docs.append(obj)

    session.add = capture_add

    with (
        patch(PIPELINE_PATCHES["extract_text"], return_value=_CONTENT),
        patch(PIPELINE_PATCHES["chunk_text"], return_value=_CHUNKS),
        patch(PIPELINE_PATCHES["embed_texts"], new_callable=AsyncMock, return_value=_EMBEDDINGS),
    ):
        await ingest_document(session, doc_data, connector_id=connector_id)

    # The first call to add should be the Document
    assert len(created_docs) >= 1
    document_obj = created_docs[0]
    assert document_obj.connector_id == connector_id


# ============================================================
# Module 2 — retrieval.hybrid_search
# ============================================================


def _make_mock_row(row_dict: dict) -> MagicMock:
    """Return a mock that mimics SQLAlchemy Row with ._mapping."""
    row = MagicMock()
    row._mapping = row_dict
    return row


@pytest.mark.asyncio
async def test_vector_search_returns_dicts_from_rows():
    """vector_search maps each result row to a dict via row._mapping."""
    from aidomaincontext.retrieval.hybrid_search import vector_search

    chunk_id = uuid.uuid4()
    row_data = {
        "id": chunk_id,
        "document_id": uuid.uuid4(),
        "chunk_index": 0,
        "content": "vector result",
        "token_count": 5,
        "score": 0.9,
    }
    mock_row = _make_mock_row(row_data)

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([mock_row]))
    session.execute = AsyncMock(return_value=mock_result)

    embedding = [0.1, 0.2, 0.3]
    results = await vector_search(session, embedding, top_k=10)

    session.execute.assert_awaited_once()
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0] == row_data
    assert isinstance(results[0], dict)


@pytest.mark.asyncio
async def test_vector_search_empty_result():
    """vector_search returns empty list when no rows match."""
    from aidomaincontext.retrieval.hybrid_search import vector_search

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([]))
    session.execute = AsyncMock(return_value=mock_result)

    results = await vector_search(session, [0.1, 0.2], top_k=5)

    assert results == []


@pytest.mark.asyncio
async def test_bm25_search_returns_dicts_from_rows():
    """bm25_search maps each result row to a dict via row._mapping."""
    from aidomaincontext.retrieval.hybrid_search import bm25_search

    chunk_id = uuid.uuid4()
    row_data = {
        "id": chunk_id,
        "document_id": uuid.uuid4(),
        "chunk_index": 1,
        "content": "bm25 result text",
        "token_count": 4,
        "score": 0.75,
    }
    mock_row = _make_mock_row(row_data)

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([mock_row]))
    session.execute = AsyncMock(return_value=mock_result)

    results = await bm25_search(session, "bm25 result", top_k=10)

    session.execute.assert_awaited_once()
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0] == row_data


@pytest.mark.asyncio
async def test_bm25_search_empty_result():
    """bm25_search returns empty list when no rows match."""
    from aidomaincontext.retrieval.hybrid_search import bm25_search

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([]))
    session.execute = AsyncMock(return_value=mock_result)

    results = await bm25_search(session, "nothing", top_k=5)

    assert results == []


@pytest.mark.asyncio
async def test_hybrid_search_fuses_results_and_truncates_to_rerank_top_k():
    """hybrid_search calls embed_query, vector_search, bm25_search, applies RRF, and
    truncates the result to settings.rerank_top_k."""
    from aidomaincontext.retrieval import hybrid_search as hs_module

    shared_id = uuid.uuid4()
    only_vector_id = uuid.uuid4()
    only_bm25_id = uuid.uuid4()

    vector_rows = [
        _make_chunk_dict(chunk_id=shared_id, score=0.9),
        _make_chunk_dict(chunk_id=only_vector_id, score=0.5),
    ]
    bm25_rows = [
        _make_chunk_dict(chunk_id=shared_id, score=0.8),
        _make_chunk_dict(chunk_id=only_bm25_id, score=0.4),
    ]

    session = AsyncMock()
    fake_embedding = [0.1] * 10

    with (
        patch("aidomaincontext.retrieval.hybrid_search.embed_query", new_callable=AsyncMock, return_value=fake_embedding) as mock_embed_query,
        patch("aidomaincontext.retrieval.hybrid_search.vector_search", new_callable=AsyncMock, return_value=vector_rows) as mock_vs,
        patch("aidomaincontext.retrieval.hybrid_search.bm25_search", new_callable=AsyncMock, return_value=bm25_rows) as mock_bm25,
        patch.object(hs_module.settings, "search_top_k", 10),
        patch.object(hs_module.settings, "rerank_top_k", 2),
    ):
        results = await hs_module.hybrid_search(session, "my query", top_k=10)

    mock_embed_query.assert_awaited_once_with("my query")
    mock_vs.assert_awaited_once_with(
        session, fake_embedding, 10,
        connector_id=None, source_type=None, author=None, date_from=None, date_to=None,
    )
    mock_bm25.assert_awaited_once_with(
        session, "my query", 10,
        connector_id=None, source_type=None, author=None, date_from=None, date_to=None,
    )

    # Result must be truncated to rerank_top_k=2
    assert len(results) <= 2

    # The chunk that appeared in both lists should rank highest (RRF boost)
    assert results[0]["id"] == shared_id


@pytest.mark.asyncio
async def test_hybrid_search_uses_settings_search_top_k_when_top_k_not_provided():
    """When top_k=None, hybrid_search falls back to settings.search_top_k."""
    from aidomaincontext.retrieval import hybrid_search as hs_module

    session = AsyncMock()
    fake_embedding = [0.0] * 5

    with (
        patch("aidomaincontext.retrieval.hybrid_search.embed_query", new_callable=AsyncMock, return_value=fake_embedding),
        patch("aidomaincontext.retrieval.hybrid_search.vector_search", new_callable=AsyncMock, return_value=[]) as mock_vs,
        patch("aidomaincontext.retrieval.hybrid_search.bm25_search", new_callable=AsyncMock, return_value=[]),
        patch.object(hs_module.settings, "search_top_k", 99),
        patch.object(hs_module.settings, "rerank_top_k", 5),
    ):
        await hs_module.hybrid_search(session, "query")

    # vector_search called with the settings fallback top_k
    _, call_kwargs = mock_vs.call_args
    positional_args = mock_vs.call_args[0]
    # third positional arg is top_k
    assert positional_args[2] == 99


@pytest.mark.asyncio
async def test_hybrid_search_empty_results_when_both_searches_return_nothing():
    """When both searches return empty lists, hybrid_search returns []."""
    from aidomaincontext.retrieval import hybrid_search as hs_module

    session = AsyncMock()

    with (
        patch("aidomaincontext.retrieval.hybrid_search.embed_query", new_callable=AsyncMock, return_value=[0.0]),
        patch("aidomaincontext.retrieval.hybrid_search.vector_search", new_callable=AsyncMock, return_value=[]),
        patch("aidomaincontext.retrieval.hybrid_search.bm25_search", new_callable=AsyncMock, return_value=[]),
        patch.object(hs_module.settings, "search_top_k", 10),
        patch.object(hs_module.settings, "rerank_top_k", 5),
    ):
        results = await hs_module.hybrid_search(session, "nothing here")

    assert results == []


# ============================================================
# Module 3 — sync.arq_worker
# ============================================================


def _make_sync_job_mock(
    job_id: uuid.UUID | None = None,
    status: str = "completed",
    documents_synced: int = 3,
    documents_failed: int = 0,
) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.status = status
    job.documents_synced = documents_synced
    job.documents_failed = documents_failed
    return job


@pytest.mark.asyncio
async def test_sync_connector_task_calls_run_sync_job_with_correct_uuid_and_sync_type():
    """sync_connector_task converts string connector_id to UUID and passes sync_type."""
    from aidomaincontext.sync.arq_worker import sync_connector_task

    connector_id = uuid.uuid4()
    job_mock = _make_sync_job_mock()

    with patch(
        "aidomaincontext.sync.arq_worker.run_sync_job",
        new_callable=AsyncMock,
        return_value=job_mock,
    ) as mock_run:
        result = await sync_connector_task({}, str(connector_id), sync_type="full")

    mock_run.assert_awaited_once_with(
        connector_id=connector_id,
        sync_type="full",
    )

    assert result["sync_job_id"] == str(job_mock.id)
    assert result["status"] == job_mock.status
    assert result["documents_synced"] == job_mock.documents_synced
    assert result["documents_failed"] == job_mock.documents_failed


@pytest.mark.asyncio
async def test_sync_connector_task_default_sync_type_is_incremental():
    """When sync_type is not provided, it defaults to 'incremental'."""
    from aidomaincontext.sync.arq_worker import sync_connector_task

    connector_id = uuid.uuid4()
    job_mock = _make_sync_job_mock()

    with patch(
        "aidomaincontext.sync.arq_worker.run_sync_job",
        new_callable=AsyncMock,
        return_value=job_mock,
    ) as mock_run:
        await sync_connector_task({}, str(connector_id))

    _, call_kwargs = mock_run.call_args
    assert call_kwargs["sync_type"] == "incremental"


@pytest.mark.asyncio
async def test_sync_connector_task_returns_expected_dict_shape():
    """Return dict contains exactly the four expected keys."""
    from aidomaincontext.sync.arq_worker import sync_connector_task

    job_mock = _make_sync_job_mock(documents_synced=7, documents_failed=1, status="completed")

    with patch(
        "aidomaincontext.sync.arq_worker.run_sync_job",
        new_callable=AsyncMock,
        return_value=job_mock,
    ):
        result = await sync_connector_task({}, str(uuid.uuid4()))

    assert set(result.keys()) == {"sync_job_id", "status", "documents_synced", "documents_failed"}
    assert result["documents_synced"] == 7
    assert result["documents_failed"] == 1
    assert result["status"] == "completed"


def test_worker_settings_functions_contains_sync_connector_task():
    """WorkerSettings.functions must include sync_connector_task."""
    from aidomaincontext.sync.arq_worker import WorkerSettings, sync_connector_task

    assert sync_connector_task in WorkerSettings.functions


def test_worker_settings_job_timeout_is_600():
    """WorkerSettings.job_timeout must be exactly 600 seconds."""
    from aidomaincontext.sync.arq_worker import WorkerSettings

    assert WorkerSettings.job_timeout == 600


# ============================================================
# Module 4 — sync.scheduler
# ============================================================

SCHEDULER_PATCHES = dict(
    create_pool="aidomaincontext.sync.scheduler.create_pool",
    async_session="aidomaincontext.sync.scheduler.async_session",
    AsyncIOScheduler="aidomaincontext.sync.scheduler.AsyncIOScheduler",
)


def _make_redis_pool_mock() -> AsyncMock:
    pool = AsyncMock()
    pool.enqueue_job = AsyncMock()
    pool.close = AsyncMock()
    return pool


def _make_session_cm(connector_ids: list[uuid.UUID]) -> tuple[AsyncMock, AsyncMock]:
    """Return (session_cm, session) mocks where scalars().all() yields connector_ids."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = connector_ids

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return session_cm, session


@pytest.mark.asyncio
async def test_enqueue_sync_creates_pool_enqueues_job_and_closes():
    """_enqueue_sync creates an arq pool, calls enqueue_job, then closes the pool."""
    from aidomaincontext.sync.scheduler import _enqueue_sync

    connector_id = uuid.uuid4()
    pool_mock = _make_redis_pool_mock()

    with patch(SCHEDULER_PATCHES["create_pool"], new_callable=AsyncMock, return_value=pool_mock) as mock_create:
        await _enqueue_sync(connector_id)

    mock_create.assert_awaited_once()
    pool_mock.enqueue_job.assert_awaited_once_with("sync_connector_task", str(connector_id))
    pool_mock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_sync_closes_pool_even_on_enqueue_error():
    """_enqueue_sync must close the pool in the finally block even if enqueue_job raises."""
    from aidomaincontext.sync.scheduler import _enqueue_sync

    connector_id = uuid.uuid4()
    pool_mock = _make_redis_pool_mock()
    pool_mock.enqueue_job = AsyncMock(side_effect=RuntimeError("Redis unreachable"))

    with patch(SCHEDULER_PATCHES["create_pool"], new_callable=AsyncMock, return_value=pool_mock):
        with pytest.raises(RuntimeError, match="Redis unreachable"):
            await _enqueue_sync(connector_id)

    # close() must still be called despite the error
    pool_mock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_all_enabled_connectors_queries_db_and_enqueues_each():
    """_enqueue_all_enabled_connectors calls _enqueue_sync once per enabled connector."""
    from aidomaincontext.sync import scheduler as sched_module

    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    session_cm, _ = _make_session_cm(ids)

    with (
        patch(SCHEDULER_PATCHES["async_session"], return_value=session_cm),
        patch.object(sched_module, "_enqueue_sync", new_callable=AsyncMock) as mock_enqueue,
    ):
        await sched_module._enqueue_all_enabled_connectors()

    assert mock_enqueue.await_count == len(ids)
    for cid in ids:
        mock_enqueue.assert_any_await(cid)


@pytest.mark.asyncio
async def test_enqueue_all_enabled_connectors_does_nothing_when_no_connectors():
    """_enqueue_all_enabled_connectors enqueues nothing when DB returns empty list."""
    from aidomaincontext.sync import scheduler as sched_module

    session_cm, _ = _make_session_cm([])

    with (
        patch(SCHEDULER_PATCHES["async_session"], return_value=session_cm),
        patch.object(sched_module, "_enqueue_sync", new_callable=AsyncMock) as mock_enqueue,
    ):
        await sched_module._enqueue_all_enabled_connectors()

    mock_enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_stale_connectors_enqueues_stale_only():
    """_enqueue_stale_connectors calls _enqueue_sync only for stale connector IDs."""
    from aidomaincontext.sync import scheduler as sched_module

    stale_ids = [uuid.uuid4(), uuid.uuid4()]
    session_cm, _ = _make_session_cm(stale_ids)

    with (
        patch(SCHEDULER_PATCHES["async_session"], return_value=session_cm),
        patch.object(sched_module, "_enqueue_sync", new_callable=AsyncMock) as mock_enqueue,
    ):
        await sched_module._enqueue_stale_connectors()

    assert mock_enqueue.await_count == len(stale_ids)
    for cid in stale_ids:
        mock_enqueue.assert_any_await(cid)


@pytest.mark.asyncio
async def test_enqueue_stale_connectors_no_op_when_all_recent():
    """_enqueue_stale_connectors does nothing when DB returns no stale connectors."""
    from aidomaincontext.sync import scheduler as sched_module

    session_cm, _ = _make_session_cm([])

    with (
        patch(SCHEDULER_PATCHES["async_session"], return_value=session_cm),
        patch.object(sched_module, "_enqueue_sync", new_callable=AsyncMock) as mock_enqueue,
    ):
        await sched_module._enqueue_stale_connectors()

    mock_enqueue.assert_not_awaited()


def test_start_scheduler_returns_scheduler_with_two_jobs():
    """start_scheduler adds exactly two jobs and starts the scheduler."""
    from aidomaincontext.sync import scheduler as sched_module

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)

    with patch(SCHEDULER_PATCHES["AsyncIOScheduler"], mock_scheduler_cls):
        result = sched_module.start_scheduler()

    assert result is mock_scheduler
    assert mock_scheduler.add_job.call_count == 2
    mock_scheduler.start.assert_called_once()

    # Verify job IDs match expected
    job_ids = {call.kwargs["id"] for call in mock_scheduler.add_job.call_args_list}
    assert "sync_all_connectors" in job_ids
    assert "startup_stale_sync" in job_ids


def test_start_scheduler_sets_module_level_scheduler():
    """start_scheduler stores the scheduler in the module-level _scheduler variable."""
    from aidomaincontext.sync import scheduler as sched_module

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)

    original = sched_module._scheduler
    try:
        with patch(SCHEDULER_PATCHES["AsyncIOScheduler"], mock_scheduler_cls):
            sched_module.start_scheduler()

        assert sched_module._scheduler is mock_scheduler
    finally:
        sched_module._scheduler = original


def test_stop_scheduler_calls_shutdown_and_clears_reference():
    """stop_scheduler calls shutdown(wait=False) and sets _scheduler to None."""
    from aidomaincontext.sync import scheduler as sched_module

    mock_scheduler = MagicMock()
    sched_module._scheduler = mock_scheduler

    sched_module.stop_scheduler()

    mock_scheduler.shutdown.assert_called_once_with(wait=False)
    assert sched_module._scheduler is None


def test_stop_scheduler_is_noop_when_already_none():
    """stop_scheduler does nothing and does not raise when _scheduler is already None."""
    from aidomaincontext.sync import scheduler as sched_module

    sched_module._scheduler = None

    # Should not raise
    sched_module.stop_scheduler()

    assert sched_module._scheduler is None
