from typing import Literal

from pydantic import BaseModel

from aidomaincontext.schemas.documents import ChunkResponse


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


class SearchResult(BaseModel):
    chunks: list[ChunkResponse]
    query: str


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5
    session_id: str | None = None


class Citation(BaseModel):
    document_title: str
    document_url: str | None
    chunk_content: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    query: str
    session_id: str
