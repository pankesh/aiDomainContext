import tempfile

import structlog
from fastapi import APIRouter, Depends, UploadFile

from aidomaincontext.connectors.file_upload import FileUploadConnector
from aidomaincontext.ingestion.pipeline import ingest_document
from aidomaincontext.models.database import get_session
from aidomaincontext.schemas.documents import DocumentResponse

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1", tags=["upload"])


@router.post("/upload", response_model=DocumentResponse)
async def upload_file(file: UploadFile, session=Depends(get_session)):
    connector = FileUploadConnector()
    content_bytes = await file.read()

    # For text files, pass content directly
    text_types = {
        "text/plain", "text/markdown", "text/csv", "text/html",
        "application/json", "application/xml",
    }
    content_type = file.content_type or ""

    if content_type in text_types or (file.filename and file.filename.endswith((".txt", ".md", ".csv", ".json", ".xml"))):
        doc_data = connector.create_document(file.filename or "untitled", content_bytes.decode("utf-8"))
        doc = await ingest_document(session, doc_data)
    else:
        # Write to temp file for unstructured parsing
        suffix = ""
        if file.filename and "." in file.filename:
            suffix = "." + file.filename.rsplit(".", 1)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content_bytes)
            tmp_path = tmp.name

        doc_data = connector.create_document(file.filename or "untitled", "")
        doc = await ingest_document(session, doc_data, file_path=tmp_path)

    logger.info("file_uploaded", filename=file.filename, document_id=str(doc.id))

    return DocumentResponse(
        id=doc.id,
        connector_id=doc.connector_id,
        source_id=doc.source_id,
        source_type=doc.source_type,
        title=doc.title,
        content=doc.content[:500],
        url=doc.url,
        author=doc.author,
        metadata=doc.metadata_,
        permissions=doc.permissions,
        content_hash=doc.content_hash,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )
