import hashlib
import uuid
from collections.abc import AsyncIterator

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.schemas.documents import DocumentBase


@register_connector
class FileUploadConnector:
    connector_type = "file_upload"

    async def validate_credentials(self, config: dict) -> bool:
        return True

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        raise NotImplementedError("File upload connector uses direct ingestion, not polling")

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        raise NotImplementedError("File upload connector does not support webhooks")

    def create_document(self, filename: str, content: str) -> DocumentBase:
        return DocumentBase(
            source_id=f"upload:{hashlib.sha256(filename.encode()).hexdigest()[:16]}:{uuid.uuid4().hex[:8]}",
            source_type="file_upload",
            title=filename,
            content=content,
            metadata={"filename": filename},
        )
