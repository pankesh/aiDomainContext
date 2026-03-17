import structlog
from fastapi import FastAPI

import aidomaincontext.connectors  # noqa: F401 — registers all connector implementations
from aidomaincontext.api.routes_admin import router as admin_router
from aidomaincontext.api.routes_connectors import router as connectors_router
from aidomaincontext.api.routes_search import router as search_router
from aidomaincontext.api.routes_upload import router as upload_router
from aidomaincontext.api.routes_webhooks import router as webhooks_router

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)

app = FastAPI(
    title="aiDomainContext",
    description="Enterprise RAG system for unified company knowledge search",
    version="0.1.0",
)

app.include_router(search_router)
app.include_router(upload_router)
app.include_router(admin_router)
app.include_router(connectors_router)
app.include_router(webhooks_router)
