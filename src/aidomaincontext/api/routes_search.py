import json
import uuid

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.config import settings
from aidomaincontext.generation.llm import generate_answer
from aidomaincontext.models.database import get_session
from aidomaincontext.models.document import Document
from aidomaincontext.retrieval.hybrid_search import hybrid_search
from aidomaincontext.schemas.documents import ChunkResponse
from aidomaincontext.schemas.search import ChatRequest, ChatResponse, Message, SearchRequest, SearchResult

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1", tags=["search"])

_CHAT_SESSION_KEY = "chat:session:{}"


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def _load_session_history(redis: aioredis.Redis, session_id: str) -> list[Message]:
    raw = await redis.get(_CHAT_SESSION_KEY.format(session_id))
    if raw is None:
        return []
    return [Message(**m) for m in json.loads(raw)]


async def _save_session_history(
    redis: aioredis.Redis, session_id: str, history: list[Message]
) -> None:
    await redis.setex(
        _CHAT_SESSION_KEY.format(session_id),
        settings.chat_session_ttl_seconds,
        json.dumps([m.model_dump() for m in history]),
    )


async def _enrich_chunks_with_doc_info(
    session: AsyncSession, chunks: list[dict]
) -> list[dict]:
    """Add document title and url to chunk dicts."""
    doc_ids = {c["document_id"] for c in chunks}
    if not doc_ids:
        return chunks

    result = await session.execute(select(Document).where(Document.id.in_(doc_ids)))
    docs = {d.id: d for d in result.scalars()}

    for chunk in chunks:
        doc = docs.get(chunk["document_id"])
        if doc:
            chunk["title"] = doc.title
            chunk["url"] = doc.url
    return chunks


@router.post("/search", response_model=SearchResult)
async def search(request: SearchRequest, session: AsyncSession = Depends(get_session)):
    chunks = await hybrid_search(session, request.query, request.top_k)
    chunks = await _enrich_chunks_with_doc_info(session, chunks)

    return SearchResult(
        query=request.query,
        chunks=[
            ChunkResponse(
                id=c["id"],
                document_id=c["document_id"],
                chunk_index=c["chunk_index"],
                content=c["content"],
                token_count=c["token_count"],
                score=c.get("score"),
            )
            for c in chunks
        ],
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, session: AsyncSession = Depends(get_session)):
    session_id = request.session_id or str(uuid.uuid4())

    redis = await _get_redis()
    try:
        history = await _load_session_history(redis, session_id)

        chunks = await hybrid_search(session, request.query, request.top_k)
        chunks = await _enrich_chunks_with_doc_info(session, chunks)
        answer, citations = await generate_answer(request.query, chunks, history=history)

        updated_history = history + [
            Message(role="user", content=request.query),
            Message(role="assistant", content=answer),
        ]
        await _save_session_history(redis, session_id, updated_history)
    finally:
        await redis.aclose()

    logger.info("chat.turn", session_id=session_id, history_turns=len(history))
    return ChatResponse(answer=answer, citations=citations, query=request.query, session_id=session_id)
