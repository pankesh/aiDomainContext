"""Unit tests for security.py, ingestion/parser.py, and ingestion/embedder.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet


# ================================================================== #
# Module 1: security.py
# ================================================================== #


@pytest.fixture
def valid_fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def another_fernet_key() -> str:
    return Fernet.generate_key().decode()


def test_encrypt_round_trip(valid_fernet_key):
    """encrypt_config followed by decrypt_config recovers the original dict."""
    with patch("aidomaincontext.security.settings") as mock_settings:
        mock_settings.encryption_key = valid_fernet_key

        from aidomaincontext.security import decrypt_config, encrypt_config

        original = {"token": "abc123", "workspace": "my-org"}
        stored = encrypt_config(original)
        recovered = decrypt_config(stored)

        assert recovered == original


def test_encrypt_produces_e_key(valid_fernet_key):
    """encrypt_config output must have exactly the '_e' envelope key."""
    with patch("aidomaincontext.security.settings") as mock_settings:
        mock_settings.encryption_key = valid_fernet_key

        from aidomaincontext.security import encrypt_config

        stored = encrypt_config({"foo": "bar"})

        assert "_e" in stored
        assert isinstance(stored["_e"], str)
        # The raw original key should NOT appear in plaintext
        assert "foo" not in stored


def test_decrypt_without_e_key_returns_plaintext(valid_fernet_key):
    """decrypt_config on a dict without '_e' returns it unchanged (legacy plaintext)."""
    with patch("aidomaincontext.security.settings") as mock_settings:
        mock_settings.encryption_key = valid_fernet_key

        from aidomaincontext.security import decrypt_config

        plaintext = {"token": "plain-secret"}
        result = decrypt_config(plaintext)

        assert result == plaintext


def test_decrypt_wrong_key_raises_value_error(valid_fernet_key, another_fernet_key):
    """Decrypting with a different key raises ValueError."""
    with patch("aidomaincontext.security.settings") as mock_settings:
        mock_settings.encryption_key = valid_fernet_key

        from aidomaincontext.security import encrypt_config

        stored = encrypt_config({"secret": "value"})

    # Now decrypt with a different key
    with patch("aidomaincontext.security.settings") as mock_settings:
        mock_settings.encryption_key = another_fernet_key

        from aidomaincontext.security import decrypt_config

        with pytest.raises(ValueError, match="Failed to decrypt connector config"):
            decrypt_config(stored)


def test_encrypt_round_trip_nested_values(valid_fernet_key):
    """Nested dicts, lists, and various value types survive the encrypt/decrypt cycle."""
    with patch("aidomaincontext.security.settings") as mock_settings:
        mock_settings.encryption_key = valid_fernet_key

        from aidomaincontext.security import decrypt_config, encrypt_config

        original = {
            "oauth_tokens": {
                "access_token": "ya29.xxx",
                "refresh_token": "1//yyy",
                "expiry": 1711000000,
            },
            "scopes": ["https://mail.google.com/", "openid"],
            "enabled": True,
            "threshold": 0.95,
        }
        stored = encrypt_config(original)
        recovered = decrypt_config(stored)

        assert recovered == original


# ================================================================== #
# Module 2: ingestion/parser.py
# ================================================================== #


def test_raw_text_passthrough():
    """extract_text with raw_text returns it immediately without touching partition."""
    with patch("aidomaincontext.ingestion.parser.partition") as mock_partition:
        from aidomaincontext.ingestion.parser import extract_text

        result = extract_text(raw_text="Hello, world!")

        assert result == "Hello, world!"
        mock_partition.assert_not_called()


def test_both_none_raises_value_error():
    """extract_text with neither argument raises ValueError."""
    from aidomaincontext.ingestion.parser import extract_text

    with pytest.raises(ValueError, match="Either file_path or raw_text must be provided"):
        extract_text()


def test_file_path_calls_partition():
    """extract_text with a file_path calls partition(filename=...) exactly once."""
    fake_elements = [MagicMock(__str__=lambda self: "Element one")]

    with patch("aidomaincontext.ingestion.parser.partition", return_value=fake_elements) as mock_partition:
        from aidomaincontext.ingestion.parser import extract_text

        extract_text(file_path="/tmp/test.pdf")

        mock_partition.assert_called_once_with(filename="/tmp/test.pdf")


def test_partition_elements_joined_with_double_newline():
    """extract_text joins partition results with double newlines."""
    fake_elements = [
        MagicMock(__str__=lambda self: "First paragraph"),
        MagicMock(__str__=lambda self: "Second paragraph"),
        MagicMock(__str__=lambda self: "Third paragraph"),
    ]

    with patch("aidomaincontext.ingestion.parser.partition", return_value=fake_elements):
        from aidomaincontext.ingestion.parser import extract_text

        result = extract_text(file_path="/tmp/doc.docx")

        assert result == "First paragraph\n\nSecond paragraph\n\nThird paragraph"


def test_raw_text_takes_precedence_over_file_path():
    """When both raw_text and file_path are given, raw_text wins and partition is not called."""
    with patch("aidomaincontext.ingestion.parser.partition") as mock_partition:
        from aidomaincontext.ingestion.parser import extract_text

        result = extract_text(file_path="/tmp/ignored.pdf", raw_text="Use me instead")

        assert result == "Use me instead"
        mock_partition.assert_not_called()


# ================================================================== #
# Module 3: ingestion/embedder.py
# ================================================================== #


@pytest.fixture(autouse=True)
def reset_embedder_model():
    """Reset the module-level _model singleton before every test in this file."""
    import aidomaincontext.ingestion.embedder as embedder_module

    embedder_module._model = None
    yield
    embedder_module._model = None


@pytest.mark.asyncio
async def test_embed_texts_empty_list():
    """embed_texts([]) returns [] without loading the model."""
    with patch("aidomaincontext.ingestion.embedder.SentenceTransformer") as MockST:
        from aidomaincontext.ingestion.embedder import embed_texts

        result = await embed_texts([])

        assert result == []
        MockST.assert_not_called()


@pytest.mark.asyncio
async def test_embed_texts_returns_list_of_lists():
    """embed_texts returns a list[list[float]] with one entry per input text."""
    import numpy as np

    fake_embeddings = np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], dtype="float32")

    mock_model = MagicMock()
    mock_model.encode.return_value = fake_embeddings

    with patch("aidomaincontext.ingestion.embedder.SentenceTransformer", return_value=mock_model):
        from aidomaincontext.ingestion.embedder import embed_texts

        result = await embed_texts(["hello", "world"])

        mock_model.encode.assert_called_once_with(["hello", "world"], normalize_embeddings=True)
        assert len(result) == 2
        assert result[0] == pytest.approx([0.1, 0.2, 0.3], abs=1e-5)
        assert result[1] == pytest.approx([0.4, 0.5, 0.6], abs=1e-5)
        # Each element must be a plain list, not an ndarray
        assert isinstance(result[0], list)
        assert isinstance(result[1], list)


@pytest.mark.asyncio
async def test_embed_query_returns_flat_list():
    """embed_query returns a flat list[float] for a single string."""
    import numpy as np

    fake_embedding = np.array([0.7, 0.8, 0.9], dtype="float32")

    mock_model = MagicMock()
    mock_model.encode.return_value = fake_embedding

    with patch("aidomaincontext.ingestion.embedder.SentenceTransformer", return_value=mock_model):
        from aidomaincontext.ingestion.embedder import embed_query

        result = await embed_query("what is RAG?")

        mock_model.encode.assert_called_once_with("what is RAG?", normalize_embeddings=True)
        assert isinstance(result, list)
        assert result == pytest.approx([0.7, 0.8, 0.9], abs=1e-5)


@pytest.mark.asyncio
async def test_model_is_lazy_loaded_singleton():
    """SentenceTransformer is instantiated only once regardless of how many calls are made."""
    import numpy as np

    fake_embedding = np.array([0.1, 0.2], dtype="float32")
    fake_embeddings = np.array([[0.1, 0.2]], dtype="float32")

    mock_model = MagicMock()
    mock_model.encode.side_effect = [fake_embeddings, fake_embedding]

    with patch("aidomaincontext.ingestion.embedder.SentenceTransformer", return_value=mock_model) as MockST:
        from aidomaincontext.ingestion.embedder import embed_query, embed_texts

        await embed_texts(["first call"])
        await embed_query("second call")

        # Constructor must have been called exactly once (singleton)
        MockST.assert_called_once()


@pytest.mark.asyncio
async def test_model_loaded_with_configured_model_name():
    """SentenceTransformer is initialised with the model name from settings."""
    import numpy as np

    fake_embeddings = np.array([[0.1, 0.2]], dtype="float32")
    mock_model = MagicMock()
    mock_model.encode.return_value = fake_embeddings

    with patch("aidomaincontext.ingestion.embedder.SentenceTransformer", return_value=mock_model) as MockST, \
         patch("aidomaincontext.ingestion.embedder.settings") as mock_settings:
        mock_settings.embedding_model = "BAAI/bge-base-en-v1.5"

        from aidomaincontext.ingestion.embedder import embed_texts

        await embed_texts(["probe"])

        MockST.assert_called_once_with("BAAI/bge-base-en-v1.5")
