import asyncio
import hashlib
import hmac
import json
import time
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.config import settings
from aidomaincontext.connectors.base import get_connector
from aidomaincontext.ingestion.pipeline import ingest_document
from aidomaincontext.security import decrypt_config
from aidomaincontext.models.connector import Connector
from aidomaincontext.models.database import get_session

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["webhooks"])

_SLACK_TIMESTAMP_TOLERANCE_SECONDS = 300  # 5 minutes — reject replays


def _verify_slack_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    signing_secret: str,
) -> None:
    """Raise HTTP 401 if the Slack signature is missing or invalid."""
    if not signing_secret:
        raise HTTPException(status_code=500, detail="Slack signing secret not configured")

    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")

    # Reject requests older than the tolerance window (replay protection)
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp")
    if abs(time.time() - ts) > _SLACK_TIMESTAMP_TOLERANCE_SECONDS:
        raise HTTPException(status_code=401, detail="Request timestamp too old")

    base_string = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), base_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


def _verify_github_signature(
    body: bytes,
    signature: str | None,
    webhook_secret: str,
) -> None:
    """Raise HTTP 401 if the GitHub signature is missing or invalid."""
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="GitHub webhook secret not configured")

    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    expected = "sha256=" + hmac.new(
        webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")


async def _process_webhook_documents(connector: Connector, documents: list) -> None:
    """Ingest webhook documents in the background."""
    from aidomaincontext.models.database import async_session

    async with async_session() as session:
        for doc_data in documents:
            try:
                await ingest_document(session, doc_data, connector_id=connector.id)
            except Exception:
                logger.exception(
                    "webhook_document_ingestion_failed",
                    connector_id=str(connector.id),
                    source_id=getattr(doc_data, "source_id", None),
                )


async def _resolve_connector(
    session: AsyncSession,
    connector_id_header: str | None,
) -> Connector:
    """Resolve connector from header, raising 404 if not found."""
    if not connector_id_header:
        raise HTTPException(status_code=400, detail="X-Connector-Id header is required")

    try:
        cid = UUID(connector_id_header)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connector ID format")

    connector = await session.get(Connector, cid)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


@router.post("/webhooks/slack")
async def handle_slack_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_connector_id: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
):
    body = await request.body()

    # Verify signature before parsing payload
    _verify_slack_signature(
        body,
        x_slack_request_timestamp,
        x_slack_signature,
        settings.slack_signing_secret,
    )

    payload = json.loads(body)

    # Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    # Resolve connector — try header first, then match by workspace
    connector: Connector | None = None
    if x_connector_id:
        connector = await _resolve_connector(session, x_connector_id)
    else:
        # Attempt to find connector by Slack workspace/team ID
        team_id = payload.get("team_id")
        if team_id:
            result = await session.execute(
                select(Connector).where(
                    Connector.connector_type == "slack",
                    Connector.enabled.is_(True),
                )
            )
            for c in result.scalars().all():
                config = decrypt_config(c.config_encrypted or {})
                if config.get("team_id") == team_id or config.get("workspace_id") == team_id:
                    connector = c
                    break

    if not connector:
        raise HTTPException(status_code=404, detail="No matching Slack connector found")

    # Dispatch processing in background
    connector_impl = get_connector(connector.connector_type)
    try:
        documents = await connector_impl.handle_webhook(payload)
    except Exception:
        logger.exception("slack_webhook_handle_failed", connector_id=str(connector.id))
        raise HTTPException(status_code=500, detail="Failed to process webhook payload")

    if documents:
        asyncio.create_task(_process_webhook_documents(connector, documents))

    logger.info(
        "slack_webhook_received",
        connector_id=str(connector.id),
        document_count=len(documents),
    )
    return {"ok": True}


@router.post("/webhooks/github")
async def handle_github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_connector_id: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    body = await request.body()

    # Verify signature before processing
    _verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret)

    payload = json.loads(body)

    connector = await _resolve_connector(session, x_connector_id)

    # Include event type in payload for the connector to use
    webhook_payload = {**payload, "_github_event": x_github_event}

    connector_impl = get_connector(connector.connector_type)
    try:
        documents = await connector_impl.handle_webhook(webhook_payload)
    except Exception:
        logger.exception("github_webhook_handle_failed", connector_id=str(connector.id))
        raise HTTPException(status_code=500, detail="Failed to process webhook payload")

    if documents:
        asyncio.create_task(_process_webhook_documents(connector, documents))

    logger.info(
        "github_webhook_received",
        connector_id=str(connector.id),
        github_event=x_github_event,
        document_count=len(documents),
    )
    return {"ok": True}
