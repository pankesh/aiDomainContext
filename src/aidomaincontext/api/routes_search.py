import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.generation.llm import generate_answer
from aidomaincontext.models.database import get_session
from aidomaincontext.models.document import Document
from aidomaincontext.retrieval.hybrid_search import hybrid_search
from aidomaincontext.schemas.documents import ChunkResponse
from aidomaincontext.schemas.search import ChatRequest, ChatResponse, SearchRequest, SearchResult

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1", tags=["search"])


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
    chunks = await hybrid_search(session, request.query, request.top_k)
    chunks = await _enrich_chunks_with_doc_info(session, chunks)
    return await generate_answer(request.query, chunks)
