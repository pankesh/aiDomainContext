import structlog
from sentence_transformers import SentenceTransformer

from aidomaincontext.config import settings

logger = structlog.get_logger()

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("loading_embedding_model", model=settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
    return _model


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts locally via sentence-transformers."""
    if not texts:
        return []

    model = _get_model()
    logger.info("embedding_batch", count=len(texts))
    embeddings = model.encode(texts, normalize_embeddings=True)
    return [e.tolist() for e in embeddings]


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    model = _get_model()
    embedding = model.encode(query, normalize_embeddings=True)
    return embedding.tolist()
