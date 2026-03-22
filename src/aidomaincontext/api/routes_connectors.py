import asyncio
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.connectors.base import get_connector
from aidomaincontext.models.connector import Connector
from aidomaincontext.security import encrypt_config
from aidomaincontext.models.database import get_session
from aidomaincontext.models.sync_job import SyncJob
from aidomaincontext.schemas.connectors import (
    ConnectorCreate,
    ConnectorResponse,
    ConnectorUpdate,
    SyncJobResponse,
)
from aidomaincontext.sync.worker import run_sync_job

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["connectors"])


@router.get("/connectors", response_model=list[ConnectorResponse])
async def list_connectors(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Connector).order_by(Connector.created_at.desc()))
    return result.scalars().all()


@router.post("/connectors", response_model=ConnectorResponse, status_code=201)
async def create_connector(
    body: ConnectorCreate,
    session: AsyncSession = Depends(get_session),
):
    # Validate credentials via the connector implementation
    try:
        connector_impl = get_connector(body.connector_type)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown connector type: {body.connector_type}")

    try:
        valid = await connector_impl.validate_credentials(body.config)
    except Exception as exc:
        logger.error("credential_validation_error", error=str(exc))
        raise HTTPException(status_code=400, detail=f"Credential validation failed: {exc}")

    if not valid:
        raise HTTPException(status_code=400, detail="Invalid credentials")

    connector = Connector(
        name=body.name,
        connector_type=body.connector_type,
        config_encrypted=encrypt_config(body.config),
        enabled=body.enabled,
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    logger.info("connector_created", connector_id=str(connector.id), type=body.connector_type)
    return connector


@router.post("/connectors/sync-all", response_model=list[SyncJobResponse], status_code=202)
async def trigger_sync_all(
    sync_type: str = Query(default="incremental"),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Connector).where(Connector.enabled.is_(True))
    )
    connectors = result.scalars().all()

    jobs: list[SyncJob] = []
    for connector in connectors:
        job = SyncJob(connector_id=connector.id, sync_type=sync_type, status="pending")
        session.add(job)
        jobs.append(job)

    await session.commit()
    for job in jobs:
        await session.refresh(job)
        asyncio.create_task(run_sync_job(job.connector_id, sync_type=sync_type))

    logger.info("sync_all_triggered", connector_count=len(jobs), sync_type=sync_type)
    return jobs


@router.get("/connectors/{connector_id}", response_model=ConnectorResponse)
async def get_connector_by_id(
    connector_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    connector = await session.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


@router.put("/connectors/{connector_id}", response_model=ConnectorResponse)
async def update_connector(
    connector_id: UUID,
    body: ConnectorUpdate,
    session: AsyncSession = Depends(get_session),
):
    connector = await session.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    if body.name is not None:
        connector.name = body.name
    if body.config is not None:
        connector.config_encrypted = encrypt_config(body.config)
    if body.enabled is not None:
        connector.enabled = body.enabled

    await session.commit()
    await session.refresh(connector)
    logger.info("connector_updated", connector_id=str(connector_id))
    return connector


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    connector = await session.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    await session.delete(connector)
    await session.commit()
    logger.info("connector_deleted", connector_id=str(connector_id))


@router.post("/connectors/{connector_id}/sync", response_model=SyncJobResponse, status_code=202)
async def trigger_sync(
    connector_id: UUID,
    sync_type: str = Query(default="incremental"),
    session: AsyncSession = Depends(get_session),
):
    connector = await session.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if not connector.enabled:
        raise HTTPException(status_code=400, detail="Connector is disabled")

    # Create a pending sync job record to return immediately
    sync_job = SyncJob(
        connector_id=connector_id,
        sync_type=sync_type,
        status="pending",
    )
    session.add(sync_job)
    await session.commit()
    await session.refresh(sync_job)

    # Run the actual sync in the background
    asyncio.create_task(run_sync_job(connector_id, sync_type=sync_type))
    logger.info("sync_triggered", connector_id=str(connector_id), sync_type=sync_type)
    return sync_job


@router.get("/connectors/{connector_id}/jobs", response_model=list[SyncJobResponse])
async def list_sync_jobs(
    connector_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    # Verify connector exists
    connector = await session.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")

    result = await session.execute(
        select(SyncJob)
        .where(SyncJob.connector_id == connector_id)
        .order_by(SyncJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()
