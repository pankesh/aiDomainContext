from aidomaincontext.ingestion.chunker import chunk_text, count_tokens


def test_count_tokens():
    tokens = count_tokens("Hello, world!")
    assert tokens > 0
    assert isinstance(tokens, int)


def test_chunk_short_text():
    """Text shorter than chunk size should return a single chunk."""
    text = "This is a short text that fits in one chunk."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["content"] == text
    assert chunks[0]["token_count"] > 0


def test_chunk_long_text():
    """Long text should be split into multiple overlapping chunks."""
    # Generate text that's definitely > 512 tokens
    text = " ".join(["The quick brown fox jumps over the lazy dog."] * 200)
    chunks = chunk_text(text)
    assert len(chunks) > 1

    # Verify chunk indices are sequential
    for i, chunk in enumerate(chunks):
        assert chunk["chunk_index"] == i

    # Verify each chunk is within token limit (with some tolerance for encoding)
    for chunk in chunks:
        assert chunk["token_count"] <= 520  # small tolerance


def test_chunk_empty_text():
    text = ""
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0]["token_count"] == 0 or chunks[0]["content"] == ""


def test_chunk_overlap():
    """Verify chunks have overlapping content."""
    text = " ".join(["word"] * 1500)  # Well over 512 tokens
    chunks = chunk_text(text)
    assert len(chunks) >= 2

    # Last part of chunk 0 should appear in start of chunk 1
    # (due to overlap, they share some content)
    c0_words = chunks[0]["content"].split()
    c1_words = chunks[1]["content"].split()
    # Some words near end of c0 should appear at start of c1
    overlap_found = any(w in c1_words[:100] for w in c0_words[-100:])
    assert overlap_found
