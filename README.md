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

Yahoo Mail uses OAuth 2.0 with Yahoo's authorization server. You need a Yahoo Developer app to obtain OAuth credentials.

### 1. Create a Yahoo Developer app

1. Go to [Yahoo Developer Console](https://developer.yahoo.com/apps/) and sign in
2. Click **Create an App**
3. Fill in the app details:
   - **Application Name**: your app name
   - **Application Type**: **Web Application**
   - **Callback Domain**: `localhost`
4. Under **API Permissions**, enable **Mail** → select **Read**
5. Click **Create App**
6. Copy the **Client ID (Consumer Key)** and **Client Secret (Consumer Secret)**

### 2. Configure the redirect URI

In your app settings, add the redirect URI:
```
http://localhost:8000/api/v1/oauth/yahoo/callback
```

### 3. Add credentials to `.env`

```bash
YAHOO_OAUTH_CLIENT_ID=<your_client_id>
YAHOO_OAUTH_CLIENT_SECRET=<your_client_secret>
YAHOO_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/oauth/yahoo/callback
```

### 4. Connect an account

Visit in your browser:

```
http://localhost:8000/api/v1/oauth/yahoo/authorize?connector_name=My+Yahoo+Mail
```

Complete the Yahoo consent screen. You will be redirected back and receive a `201` JSON response containing the `connector_id`.

### 5. Trigger a sync

```bash
# Full sync (first run — fetches entire inbox)
curl -X POST "http://localhost:8000/api/v1/connectors/<connector_id>/sync?sync_type=full"

# Incremental sync (subsequent runs — only messages since last sync)
curl -X POST "http://localhost:8000/api/v1/connectors/<connector_id>/sync?sync_type=incremental"
```

Incremental syncs use a `last_sync_at` timestamp cursor stored on the connector record. Only messages received after the previous sync are fetched.

### How it works

| Field | Value |
|---|---|
| `connector_type` | `yahoo_mail` |
| `source_type` | `yahoo_message` |
| `source_id` format | `yahoo_mail:<email>:<message_id>` |
| Folder synced | Inbox |
| Webhook support | No (poll-only) |

---

## Full Reset (local dev)

Wipes the database and starts fresh:

```bash
docker compose down -v
docker compose up -d
uv run alembic upgrade head
```
