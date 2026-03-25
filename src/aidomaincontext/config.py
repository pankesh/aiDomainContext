from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = (
        "postgresql+asyncpg://aidomaincontext:localdev@localhost:5432/aidomaincontext"
    )

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Connector credential encryption (Fernet key — generate with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    encryption_key: str = "4B4hk77zpRUvSIp6-LOWSaROegsHjZ0H74S9PsCldwM="

    # Hugging Face (optional — suppresses rate-limit warnings on model downloads)
    hf_token: str = ""

    # Google (embeddings — optional, only needed if using Gemini embedder)
    google_api_key: str = ""
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dimensions: int = 768

    # Anthropic
    anthropic_api_key: str = ""
    generation_model: str = "claude-sonnet-4-6"

    # Chunking
    chunk_size_tokens: int = 512
    chunk_overlap_fraction: float = 0.1

    # Retrieval
    search_top_k: int = 40
    rerank_top_k: int = 5
    context_token_budget: int = 8000

    # Chat sessions (Redis-backed)
    chat_session_ttl_seconds: int = 7200  # 2 hours sliding TTL

    # Google OAuth (Gmail connector)
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:8000/api/v1/oauth/google/callback"

    # Webhook signature verification
    slack_signing_secret: str = ""
    github_webhook_secret: str = ""



settings = Settings()
