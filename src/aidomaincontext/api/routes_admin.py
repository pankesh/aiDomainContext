from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.models.chunk import Chunk
from aidomaincontext.models.database import get_session
from aidomaincontext.models.document import Document
from aidomaincontext.schemas.documents import DocumentResponse

router = APIRouter(prefix="/api/v1", tags=["admin"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    doc_count = await session.scalar(select(func.count(Document.id)))
    chunk_count = await session.scalar(select(func.count(Chunk.id)))
    return {
        "documents": doc_count,
        "chunks": chunk_count,
    }


@router.get("/documents", response_model=list[DocumentResponse])
async def list_documents(
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Document).order_by(Document.created_at.desc()).limit(limit).offset(offset)
    )
    docs = result.scalars().all()
    return [
        DocumentResponse(
            id=d.id,
            connector_id=d.connector_id,
            source_id=d.source_id,
            source_type=d.source_type,
            title=d.title,
            content=d.content[:500],
            url=d.url,
            author=d.author,
            metadata=d.metadata_,
            permissions=d.permissions,
            content_hash=d.content_hash,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )
        for d in docs
    ]
