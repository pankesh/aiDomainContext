# aiDomainContext

Enterprise RAG system that ingests data from multiple sources (Slack, GitHub, Gmail, Google Drive, Yahoo Mail, Jira, file uploads) into a unified vector store, enabling semantic search and AI-powered Q&A across all company knowledge.

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
Data Sources → Connectors → Ingestion Pipeline → PostgreSQL (pgvector)
                                                        ↕
                                              Hybrid Search Engine
                                                        ↕
                                              Claude LLM → Chat API
```

```
src/aidomaincontext/
├── api/           # FastAPI route handlers
├── connectors/    # Data source connectors (Slack, GitHub, Gmail, Google Drive, Yahoo Mail, Jira, file upload)
├── ingestion/     # Pipeline: parse → chunk → embed → upsert
├── retrieval/     # Hybrid search (vector + BM25 + RRF fusion)
├── generation/    # Claude LLM wrapper with citation support
├── models/        # SQLAlchemy ORM models
├── schemas/       # Pydantic request/response models
└── sync/          # Background worker + scheduler (arq + APScheduler)
```

### Database (PostgreSQL + pgvector)

Everything lives in one Postgres instance. Three core tables:

**`connectors`** — one row per connected data source
- `connector_type`: `slack`, `github`, `gmail`, `google_drive`, `yahoo_mail`, `jira`, `file_upload`
- `config_encrypted`: credentials stored as a Fernet-encrypted JSON blob — never plaintext
- `sync_cursor`: JSONB dict tracking "where we left off" for incremental syncs (e.g. `{"last_sync_at": "..."}`)
- `enabled`: pause syncing without deleting the connector

**`documents`** — one row per ingested piece of content (email, issue, file, Slack message, etc.)
- `source_id` + `connector_id` form a unique key — deduplication happens here
- `content_hash`: SHA-256 of the text — if unchanged on re-sync, the document is skipped entirely
- `metadata_`: flexible JSONB for source-specific fields (e.g. Jira issue key, email message-id)

**`chunks`** — one row per text chunk split from a document
- `embedding`: a `Vector(768)` column — this is the pgvector column storing the 768-dim float array
- `chunk_index`: ordering within the parent document
- Foreign key with `ondelete=CASCADE` — deleting a document auto-deletes its chunks

### Vector Store (pgvector)

There is no separate vector database. **PostgreSQL itself is the vector store** via the `pgvector` extension.

The `<=>` operator is cosine distance — `1 - distance` gives the similarity score:

```sql
ORDER BY c.embedding <=> CAST(:embedding AS vector)
```

**Embeddings are generated locally** using `sentence-transformers` with `BAAI/bge-base-en-v1.5` (768 dimensions). This runs in-process — no external embedding API, no per-embed cost.

### Ingestion Pipeline

Every sync flows through `ingestion/pipeline.py`:

```
raw content
    → extract_text()    # unstructured: PDFs, DOCX, HTML, plain text
    → chunk_text()      # recursive token splitter: 512 tokens, 10% overlap
    → embed_texts()     # sentence-transformers: batches of 64 → 768-dim vectors
    → upsert to DB      # dedup by source_id+connector_id, skip if hash unchanged
```

The chunker splits on paragraph → sentence → word boundaries to avoid cutting mid-sentence. Each chunk gets its own embedding and its own row in `chunks`.

### Hybrid Search

Two searches run against the `chunks` table and their results are fused:

**Vector search (pgvector)** — semantic similarity
- Embeds the query with the same local model
- Finds chunks whose embedding is closest (cosine distance)
- Good at: synonyms, paraphrasing, conceptual matches

**BM25 search (PostgreSQL full-text)** — keyword matching
- Uses `to_tsvector` / `plainto_tsquery` with `ts_rank_cd` scoring
- Good at: exact terms, names, IDs, acronyms

**Reciprocal Rank Fusion (RRF)** merges both ranked lists:
```
score = Σ  1 / (60 + rank)    # summed across each result list
```
A chunk ranking high in both lists scores much higher than one appearing in only one. Returns top 5 results (`rerank_top_k`).

### Generation (Claude)

`generation/llm.py` builds a prompt from the top-k chunks and sends it to Claude:

```
[Source 1] (from: Email Subject)
...chunk text...

[Source 2] (from: Jira Issue PROJ-123)
...chunk text...

Question: <user query>
Answer based on context above. Cite sources using [Source N] notation.
```

Claude returns an answer with inline citations. The route parses which `[Source N]` references appear and builds a `citations` list pointing back to the original documents.

Conversation history is stored in Redis under `chat:session:<uuid>` with a 2-hour sliding TTL. Only raw Q&A pairs are stored — not the RAG context — to keep Redis memory small.

### Chat Request Flow

```
POST /api/v1/chat
  → load session history from Redis
  → hybrid_search(query)
      → embed_query()              # local sentence-transformers model
      → vector_search()            # pgvector cosine similarity
      → bm25_search()              # PostgreSQL full-text search
      → reciprocal_rank_fusion()   # merge + rerank
  → generate_answer(query, chunks, history)
      → build prompt with retrieved context
      → Claude API (claude-sonnet-4-6)
      → parse [Source N] citations
  → save updated history to Redis
  → return { answer, citations, session_id }
```

### Connectors

Each connector implements three methods defined by `ConnectorProtocol`:

| Method | Purpose |
|---|---|
| `validate_credentials(config)` | Test if credentials work before saving |
| `fetch_documents(config, cursor)` | Async generator yielding `(DocumentBase, new_cursor)` |
| `handle_webhook(payload)` | Parse push events into documents (or return `[]`) |

The **cursor pattern** drives incremental sync: after each yielded document the connector returns an updated cursor dict. The worker saves it on the `Connector` record. On the next run the cursor is passed back in and only new content is fetched.

### Background Jobs

**arq** (Redis-backed job queue) — sync jobs are dispatched as arq tasks, picked up by a separate worker process running `run_sync_job()`. Each job creates a `sync_jobs` DB row tracking status, document count, and errors.

**APScheduler** — runs inside the server process and fires periodic incremental syncs for all enabled connectors on a configurable schedule.

## Google Connector Setup (Gmail and Drive)

Gmail and Google Drive both use Google OAuth 2.0. They share the same OAuth credentials and consent screen — the only difference is which scopes are requested.

### 1. Create a Google Cloud project and enable the APIs

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Library**
2. Search for **Gmail API** and click **Enable**
3. Search for **Google Drive API** and click **Enable**

> **Important:** Both APIs must be enabled even if you only plan to use one connector. Missing either will cause a `403 Forbidden` error when syncing.

### 2. Configure the OAuth consent screen

1. **APIs & Services** → **OAuth consent screen** → **Get Started**
2. Fill in the app name, set **Audience** to **External**
3. Add the following scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `openid`

### 3. Create OAuth credentials

1. **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth client ID**
2. Application type: **Web application**
3. Add an **Authorized redirect URI**: `http://localhost:8000/api/v1/oauth/google/callback`
4. Copy the **Client ID** and **Client Secret**

### 4. Add credentials to `.env`

```bash
GOOGLE_OAUTH_CLIENT_ID=<your_client_id>
GOOGLE_OAUTH_CLIENT_SECRET=<your_client_secret>
OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/oauth/google/callback
```

### 5. Connect an account

The OAuth flow creates the connector automatically — no separate API call needed. Visit in your browser:

**Gmail:**
```
http://localhost:8000/api/v1/oauth/google/authorize?connector_name=My+Gmail
```

**Google Drive:**
```
http://localhost:8000/api/v1/oauth/google/authorize?connector_type=google_drive&connector_name=My+Drive
```

Complete the Google consent screen. You will be redirected back and receive a `201` JSON response containing the `connector_id`.

### 6. Trigger a sync

```bash
# Full sync (first run)
curl -X POST "http://localhost:8000/api/v1/connectors/<connector_id>/sync?sync_type=full"

# Incremental sync (subsequent runs)
# Gmail uses the History API; Drive uses the Changes API
curl -X POST "http://localhost:8000/api/v1/connectors/<connector_id>/sync?sync_type=incremental"
```

---

## Yahoo Mail Connector Setup

Yahoo Mail uses **IMAP with an app-specific password**. Yahoo's Mail REST API is not available for third-party developers, so no developer app or OAuth flow is needed.

### 1. Enable IMAP in Yahoo Mail

1. Sign in to Yahoo Mail → **Settings** → **More Settings** → **Mailboxes**
2. Select your mailbox and ensure **IMAP** is enabled

### 2. Generate an app-specific password

1. Go to your [Yahoo Account Security](https://login.yahoo.com/account/security) page
2. Scroll to **App passwords** → click **Generate app password**
3. Enter a name (e.g. `aiDomainContext`) and click **Generate**
4. Copy the 16-character password (e.g. `xxxx xxxx xxxx xxxx`)

> No changes to `.env` are required — credentials are stored encrypted in the database.

### 3. Connect an account

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Yahoo Mail",
    "connector_type": "yahoo_mail",
    "config": {
      "username": "you@yahoo.com",
      "app_password": "xxxx xxxx xxxx xxxx",
      "folder": "INBOX"
    },
    "enabled": true
  }'
```

### 4. Trigger a sync

```bash
# Full sync (first run — fetches entire inbox)
curl -X POST "http://localhost:8000/api/v1/connectors/<connector_id>/sync?sync_type=full"

# Incremental sync (subsequent runs — only new messages since last sync)
curl -X POST "http://localhost:8000/api/v1/connectors/<connector_id>/sync?sync_type=incremental"
```

Incremental syncs use a `last_uid` IMAP UID cursor. Only messages with a higher UID than the last seen are fetched.

### How it works

| Field | Value |
|---|---|
| `connector_type` | `yahoo_mail` |
| `source_type` | `yahoo_message` |
| `source_id` format | `yahoo_mail:<email>:<imap_uid>` |
| Protocol | IMAP over SSL (`imap.mail.yahoo.com:993`) |
| Folder synced | `INBOX` (configurable via `folder` config key) |
| Webhook support | No (poll-only) |

---

## Full Reset (local dev)

Wipes the database and starts fresh:

```bash
docker compose down -v
docker compose up -d
uv run alembic upgrade head
```
