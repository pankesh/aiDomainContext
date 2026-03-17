# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

aiDomainContext — Enterprise RAG system that ingests data from multiple sources (Slack, GitHub, Jira, email, file uploads) into a unified vector store, enabling semantic search and AI-powered Q&A across all company knowledge.

## Tech Stack

- Python 3.13, FastAPI, SQLAlchemy (async), PostgreSQL 16 + pgvector, Redis (arq)
- OpenAI embeddings (text-embedding-3-large, 3072d), Claude API (Anthropic) for generation
- Document parsing: `unstructured`, chunking: custom recursive token splitter
- Retrieval: hybrid search (pgvector cosine + PostgreSQL BM25) → RRF fusion → reranking

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
