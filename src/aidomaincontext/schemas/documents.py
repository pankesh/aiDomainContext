import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentBase(BaseModel):
    source_id: str
    source_type: str
    title: str = ""
    content: str = ""
    url: str | None = None
    author: str | None = None
    metadata: dict = {}
    permissions: dict = {"is_public": True}


class DocumentResponse(DocumentBase):
    id: uuid.UUID
    connector_id: uuid.UUID | None
    content_hash: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChunkResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    score: float | None = None

    model_config = {"from_attributes": True}
