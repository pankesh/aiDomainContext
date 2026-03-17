import uuid

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.config import settings
from aidomaincontext.ingestion.embedder import embed_query

logger = structlog.get_logger()


async def vector_search(
    session: AsyncSession, query_embedding: list[float], top_k: int
) -> list[dict]:
    """Cosine similarity search via pgvector."""
    result = await session.execute(
        text("""
            SELECT c.id, c.document_id, c.chunk_index, c.content, c.token_count,
                   1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
            FROM chunks c
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """),
        {"embedding": str(query_embedding), "top_k": top_k},
    )
    return [dict(row._mapping) for row in result]


async def bm25_search(session: AsyncSession, query: str, top_k: int) -> list[dict]:
    """Full-text search using PostgreSQL tsvector/tsquery."""
    result = await session.execute(
        text("""
            SELECT c.id, c.document_id, c.chunk_index, c.content, c.token_count,
                   ts_rank_cd(to_tsvector('english', c.content), plainto_tsquery('english', :query)) AS score
            FROM chunks c
            WHERE to_tsvector('english', c.content) @@ plainto_tsquery('english', :query)
            ORDER BY score DESC
            LIMIT :top_k
        """),
        {"query": query, "top_k": top_k},
    )
    return [dict(row._mapping) for row in result]


def reciprocal_rank_fusion(
    result_lists: list[list[dict]], k: int = 60
) -> list[dict]:
    """Merge multiple ranked lists using RRF. Returns sorted by fused score."""
    scores: dict[uuid.UUID, float] = {}
    items: dict[uuid.UUID, dict] = {}

    for results in result_lists:
        for rank, item in enumerate(results):
            chunk_id = item["id"]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
            items[chunk_id] = item

    fused = []
    for chunk_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        entry = items[chunk_id].copy()
        entry["score"] = score
        fused.append(entry)

    return fused


async def hybrid_search(session: AsyncSession, query: str, top_k: int | None = None) -> list[dict]:
    """Run vector + BM25 search, fuse with RRF, return top results."""
    search_k = top_k or settings.search_top_k

    query_embedding = await embed_query(query)

    vector_results = await vector_search(session, query_embedding, search_k)
    bm25_results = await bm25_search(session, query, search_k)

    logger.info(
        "hybrid_search",
        vector_count=len(vector_results),
        bm25_count=len(bm25_results),
    )

    fused = reciprocal_rank_fusion([vector_results, bm25_results])
    return fused[: settings.rerank_top_k]
