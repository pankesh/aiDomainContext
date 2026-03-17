import uuid
from datetime import datetime

from pydantic import BaseModel


class ConnectorCreate(BaseModel):
    name: str
    connector_type: str
    config: dict = {}
    enabled: bool = True


class ConnectorUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None


class ConnectorResponse(BaseModel):
    id: uuid.UUID
    name: str
    connector_type: str
    enabled: bool
    sync_cursor: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SyncJobResponse(BaseModel):
    id: uuid.UUID
    connector_id: uuid.UUID
    sync_type: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    documents_synced: int
    documents_failed: int
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
