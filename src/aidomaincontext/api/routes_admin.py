from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.models.chunk import Chunk
from aidomaincontext.models.database import get_session
from aidomaincontext.models.document import Document
from aidomaincontext.schemas.documents import DocumentResponse

logger = structlog.get_logger()
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


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await session.delete(doc)
    await session.commit()
    logger.info("document_deleted", document_id=str(document_id))


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
