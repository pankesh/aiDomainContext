# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

aiDomainContext — Enterprise RAG system that ingests data from multiple sources (Slack, GitHub, Jira, email, file uploads) into a unified vector store, enabling semantic search and AI-powered Q&A across all company knowledge.

## Tech Stack

- Python 3.13, FastAPI, SQLAlchemy (async), PostgreSQL 16 + pgvector, Redis (arq + chat sessions)
- OpenAI embeddings (text-embedding-3-large, 3072d), Claude API (Anthropic) for generation
- Document parsing: `unstructured`, chunking: custom recursive token splitter
- Retrieval: hybrid search (pgvector cosine + PostgreSQL BM25) → RRF fusion → reranking
- OAuth: Google OAuth 2.0 via `google-auth-oauthlib`, state stored in Redis with 10-min TTL
- Credentials: Fernet-encrypted before persisting to `Connector` table

## Commands

```bash
# Install dependencies
uv sync

# Run dev server
uv run uvicorn aidomaincontext.main:app --reload

# Run tests
uv run pytest

# Run specific test file
uv run pytest tests/unit/test_chunker.py -v

# Start infrastructure
docker compose up -d

# Run migrations
uv run alembic upgrade head

# Generate new migration
uv run alembic revision --autogenerate -m "description"
```

## Architecture

- `src/aidomaincontext/` — main package
  - `connectors/` — data source connectors implementing `ConnectorProtocol`
  - `ingestion/` — pipeline: parse → chunk → embed → upsert
  - `retrieval/` — hybrid search (vector + BM25 + RRF fusion)
  - `generation/` — Claude LLM wrapper with citation support
  - `api/` — FastAPI route handlers
  - `models/` — SQLAlchemy ORM models
  - `schemas/` — Pydantic request/response models

## Development Workflow

Before making any changes:
1. **Always rebase onto `main` first** — `git fetch && git rebase origin/main` — to avoid merge conflicts on the PR.
2. **Run all tests before committing** — `uv run pytest tests/unit/ -q` — all must pass before any commit or PR is opened.

## Key Design Decisions

### Chat sessions — Redis-backed (Option B)
`POST /api/v1/chat` is stateful via Redis, not client-managed history arrays.
- Client sends optional `session_id`; receives one back in every response.
- Server stores `[{role, content}]` under `chat:session:<uuid>` with a 2-hour sliding TTL.
- Only raw query/answer pairs are stored — **not** the RAG-injected context — to keep Redis memory small.
- TTL is configurable: `CHAT_SESSION_TTL_SECONDS` (default 7200).
- `generate_answer(query, chunks, history)` returns `(answer, citations)` — the route owns Redis I/O and `ChatResponse` assembly.

### Connector credentials
All connector OAuth tokens are Fernet-encrypted (`cryptography` library) before being stored in the `Connector.config_encrypted` column. Never store plaintext tokens.

### Gmail connector
Uses Google OAuth 2.0 consent flow (`/api/v1/oauth/google/authorize` → callback). Full sync uses the Gmail API; incremental sync uses the Gmail History API with a cursor stored on the `Connector` record.

### Redis usage
Redis serves two purposes: arq job queue (background sync workers) and ephemeral state (OAuth CSRF tokens, chat session history). Both use `redis.asyncio` with `aioredis.from_url(settings.redis_url)` and explicit `aclose()` in a `finally` block.
