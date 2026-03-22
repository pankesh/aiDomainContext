from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

from aidomaincontext.schemas.documents import ChunkResponse


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    connector_id: UUID | None = None
    source_type: str | None = None
    author: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class SearchResult(BaseModel):
    chunks: list[ChunkResponse]
    query: str


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5
    session_id: UUID | None = None

    @field_validator("session_id", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: object) -> object:
        if v == "":
            return None
        return v


class Citation(BaseModel):
    document_title: str
    document_url: str | None
    chunk_content: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    query: str
    session_id: str
