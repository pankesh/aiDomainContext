import structlog
from anthropic import AsyncAnthropic

from aidomaincontext.config import settings
from aidomaincontext.schemas.search import Citation, Message

logger = structlog.get_logger()

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


SYSTEM_PROMPT = """You are an AI assistant that answers questions based on company knowledge.
You MUST base your answer only on the provided context. If the context does not contain
enough information to answer, say so clearly.

For every claim you make, cite the source using [Source N] notation, where N corresponds
to the context chunk number. Always include citations."""


def _build_context(chunks: list[dict]) -> str:
    parts = []
    for i, chunk in enumerate(chunks, 1):
        title = chunk.get("title", "Unknown")
        parts.append(f"[Source {i}] (from: {title})\n{chunk['content']}")
    return "\n\n---\n\n".join(parts)


async def generate_answer(
    query: str, chunks: list[dict], history: list[Message] | None = None
) -> tuple[str, list[Citation]]:
    """Generate a cited answer using Claude given retrieved chunks and optional conversation history."""
    client = _get_client()
    context = _build_context(chunks)

    current_user_message = f"""Context:
{context}

Question: {query}

Answer the question based on the context above. Cite sources using [Source N] notation."""

    # Build messages array: prior turns first, then current query with RAG context
    messages: list[dict] = [{"role": m.role, "content": m.content} for m in (history or [])]
    messages.append({"role": "user", "content": current_user_message})

    logger.info("generating_answer", query=query, context_chunks=len(chunks), history_turns=len(history or []))

    response = await client.messages.create(
        model=settings.generation_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    answer = response.content[0].text

    citations = []
    for i, chunk in enumerate(chunks, 1):
        if f"[Source {i}]" in answer:
            citations.append(Citation(
                document_title=chunk.get("title", "Unknown"),
                document_url=chunk.get("url"),
                chunk_content=chunk["content"][:200],
            ))

    return answer, citations


async def generate_answer_stream(query: str, chunks: list[dict]):
    """Stream a cited answer using Claude. Yields text chunks."""
    client = _get_client()
    context = _build_context(chunks)

    user_message = f"""Context:
{context}

Question: {query}

Answer the question based on the context above. Cite sources using [Source N] notation."""

    async with client.messages.stream(
        model=settings.generation_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
