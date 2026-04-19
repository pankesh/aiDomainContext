import hashlib
import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.ingestion.chunker import chunk_text
from aidomaincontext.ingestion.embedder import embed_texts
from aidomaincontext.ingestion.parser import extract_text
from aidomaincontext.models.chunk import Chunk
from aidomaincontext.models.document import Document
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

EMBED_BATCH_SIZE = 64


async def ingest_document(
    session: AsyncSession,
    doc_data: DocumentBase,
    file_path: str | None = None,
    connector_id: uuid.UUID | None = None,
) -> Document:
    """Full ingestion pipeline: parse → chunk → embed → upsert."""

    # 1. Extract text
    content = extract_text(file_path=file_path, raw_text=doc_data.content or None)
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # 2. Check for existing document (dedup by connector_id + source_id)
    existing = await session.execute(
        select(Document).where(
            Document.connector_id == connector_id,
            Document.source_id == doc_data.source_id,
        )
    )
    doc = existing.scalar_one_or_none()

    if doc and doc.content_hash == content_hash:
        logger.info("document_unchanged", source_id=doc_data.source_id)
        return doc

    if doc:
        # Delete old chunks for re-ingestion (bulk delete avoids lazy load)
        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        doc.content = content
        doc.content_hash = content_hash
        doc.title = doc_data.title
        doc.url = doc_data.url
        doc.author = doc_data.author
        doc.metadata_ = doc_data.metadata
        doc.permissions = doc_data.permissions
        logger.info("document_updated", source_id=doc_data.source_id)
    else:
        doc = Document(
            connector_id=connector_id,
            source_id=doc_data.source_id,
            source_type=doc_data.source_type,
            title=doc_data.title,
            content=content,
            url=doc_data.url,
            author=doc_data.author,
            metadata_=doc_data.metadata,
            permissions=doc_data.permissions,
            content_hash=content_hash,
        )
        session.add(doc)
        logger.info("document_created", source_id=doc_data.source_id)

    await session.flush()  # get doc.id

    # 3. Chunk
    chunks_data = chunk_text(content)
    logger.info("chunked", count=len(chunks_data))

    # 4. Embed in batches
    all_texts = [c["content"] for c in chunks_data]
    all_embeddings: list[list[float]] = []
    for i in range(0, len(all_texts), EMBED_BATCH_SIZE):
        batch = all_texts[i : i + EMBED_BATCH_SIZE]
        embeddings = await embed_texts(batch)
        all_embeddings.extend(embeddings)

    # 5. Create chunk records
    for chunk_data, embedding in zip(chunks_data, all_embeddings):
        chunk = Chunk(
            document_id=doc.id,
            chunk_index=chunk_data["chunk_index"],
            content=chunk_data["content"],
            token_count=chunk_data["token_count"],
            embedding=embedding,
        )
        session.add(chunk)

    await session.commit()
    logger.info("ingestion_complete", document_id=str(doc.id), chunks=len(chunks_data))
    return doc
