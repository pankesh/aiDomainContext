# aiDomainContext

Enterprise RAG system that ingests data from multiple sources (Slack, GitHub, file uploads) into a unified vector store, enabling semantic search and AI-powered Q&A across all company knowledge.

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for PostgreSQL + Redis)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)
- Python 3.13+
- Google API key (embeddings) — free at aistudio.google.com
- Anthropic API key (generation)

---

## Quick Start

```bash
# 1. Copy and fill in your API keys
cp .env.example .env

# 2. Start infrastructure (PostgreSQL + Redis)
docker compose up -d

# 3. Install dependencies
uv sync

# 4. Run database migrations
uv run alembic upgrade head

# 5. Start the API server
uv run uvicorn aidomaincontext.main:app --reload
```

API is live at **http://localhost:8000**
Interactive docs at **http://localhost:8000/docs**

---

## Command Cheat Sheet

### Infrastructure

| Action | Command |
|---|---|
| Start Postgres + Redis | `docker compose up -d` |
| Stop containers | `docker compose down` |
| Stop and wipe all data | `docker compose down -v` |
| View container status | `docker ps` |
| View container logs | `docker compose logs -f` |
| View Postgres logs only | `docker compose logs -f postgres` |

### Application Server

| Action | Command |
|---|---|
| Start dev server (auto-reload) | `uv run uvicorn aidomaincontext.main:app --reload` |
| Start prod server | `uv run uvicorn aidomaincontext.main:app --host 0.0.0.0 --port 8000 --workers 4` |
| Check server health | `curl http://localhost:8000/api/v1/health` |

### Background Worker (sync jobs)

| Action | Command |
|---|---|
| Start arq worker | `uv run arq aidomaincontext.sync.arq_worker.WorkerSettings` |

### Database Migrations

| Action | Command |
|---|---|
| Apply all migrations | `uv run alembic upgrade head` |
| Roll back one migration | `uv run alembic downgrade -1` |
| Roll back all migrations | `uv run alembic downgrade base` |
| Show current revision | `uv run alembic current` |
| Show migration history | `uv run alembic history` |
| Generate new migration | `uv run alembic revision --autogenerate -m "description"` |

### Dependencies

| Action | Command |
|---|---|
| Install / sync dependencies | `uv sync` |
| Add a new package | `uv add <package>` |
| Add a dev-only package | `uv add --dev <package>` |

### Testing

| Action | Command |
|---|---|
| Run all tests | `uv run pytest` |
| Run with verbose output | `uv run pytest -v` |
| Run a specific file | `uv run pytest tests/unit/test_chunker.py -v` |
| Run with coverage | `uv run pytest --cov=aidomaincontext` |

---

## Environment Variables

Copy `.env.example` to `.env` and set the following:

```bash
GOOGLE_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides (defaults shown)
DATABASE_URL=postgresql+asyncpg://aidomaincontext:localdev@localhost:5432/aidomaincontext
REDIS_URL=redis://localhost:6379
EMBEDDING_MODEL=models/gemini-embedding-001
EMBEDDING_DIMENSIONS=768
GENERATION_MODEL=claude-sonnet-4-6
```

---

## Architecture

```
src/aidomaincontext/
├── api/           # FastAPI route handlers
├── connectors/    # Data source connectors (Slack, GitHub, file upload)
├── ingestion/     # Pipeline: parse → chunk → embed → upsert
├── retrieval/     # Hybrid search (vector + BM25 + RRF fusion)
├── generation/    # Claude LLM wrapper with citation support
├── models/        # SQLAlchemy ORM models
├── schemas/       # Pydantic request/response models
└── sync/          # Background worker + scheduler (arq + APScheduler)
```

## Full Reset (local dev)

Wipes the database and starts fresh:

```bash
docker compose down -v
docker compose up -d
uv run alembic upgrade head
```
