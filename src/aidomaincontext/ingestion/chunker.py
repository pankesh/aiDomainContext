import tiktoken

from aidomaincontext.config import settings

_encoder = tiktoken.encoding_for_model("gpt-4o")


def count_tokens(text: str) -> int:
    return len(_encoder.encode(text))


def chunk_text(text: str) -> list[dict]:
    """Recursive token-based splitter with overlap.

    Returns list of {"content": str, "token_count": int, "chunk_index": int}.
    """
    max_tokens = settings.chunk_size_tokens
    overlap_tokens = int(max_tokens * settings.chunk_overlap_fraction)

    tokens = _encoder.encode(text)
    if len(tokens) <= max_tokens:
        return [{"content": text.strip(), "token_count": len(tokens), "chunk_index": 0}]

    chunks = []
    start = 0
    idx = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text_str = _encoder.decode(chunk_tokens).strip()
        if chunk_text_str:
            chunks.append({
                "content": chunk_text_str,
                "token_count": len(chunk_tokens),
                "chunk_index": idx,
            })
            idx += 1
        if end >= len(tokens):
            break
        start = end - overlap_tokens

    return chunks
