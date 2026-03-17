"""APScheduler periodic triggers for connector sync jobs."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from aidomaincontext.config import settings
from aidomaincontext.models.connector import Connector
from aidomaincontext.models.database import async_session
from aidomaincontext.models.sync_job import SyncJob

logger = structlog.get_logger()

_scheduler: AsyncIOScheduler | None = None


def _redis_settings() -> RedisSettings:
    """Derive arq RedisSettings from the application redis_url."""
    return RedisSettings.from_dsn(settings.redis_url)


async def _enqueue_sync(connector_id: UUID) -> None:
    """Enqueue a single sync job via arq."""
    redis = await create_pool(_redis_settings())
    try:
        await redis.enqueue_job(
            "sync_connector_task",
            str(connector_id),
        )
        logger.info("sync_enqueued", connector_id=str(connector_id))
    finally:
        await redis.close()


async def _enqueue_all_enabled_connectors() -> None:
    """Query all enabled connectors and enqueue a sync job for each."""
    async with async_session() as session:
        result = await session.execute(
            select(Connector.id).where(Connector.enabled.is_(True))
        )
        connector_ids = result.scalars().all()

    logger.info("scheduling_sync_for_connectors", count=len(connector_ids))
    for cid in connector_ids:
        await _enqueue_sync(cid)


async def _enqueue_stale_connectors() -> None:
    """Enqueue connectors that haven't synced in over 1 hour.

    A connector is considered stale if it has no completed sync job
    with a finished_at within the last hour.
    """
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

    async with async_session() as session:
        # Subquery: connector IDs with a recent completed sync
        recent_synced = (
            select(SyncJob.connector_id)
            .where(
                SyncJob.status == "completed",
                SyncJob.finished_at >= one_hour_ago,
            )
            .distinct()
            .subquery()
        )

        result = await session.execute(
            select(Connector.id).where(
                Connector.enabled.is_(True),
                Connector.id.notin_(select(recent_synced.c.connector_id)),
            )
        )
        stale_ids = result.scalars().all()

    if stale_ids:
        logger.info("enqueuing_stale_connectors", count=len(stale_ids))
        for cid in stale_ids:
            await _enqueue_sync(cid)


def start_scheduler() -> AsyncIOScheduler:
    """Create, configure, and start the APScheduler instance.

    Registers a periodic job (every 15 minutes) to sync all enabled
    connectors, and immediately enqueues any stale connectors on startup.
    """
    global _scheduler

    scheduler = AsyncIOScheduler()

    # Every 15 minutes: sync all enabled connectors
    scheduler.add_job(
        _enqueue_all_enabled_connectors,
        "interval",
        minutes=15,
        id="sync_all_connectors",
        replace_existing=True,
    )

    # On startup: enqueue stale connectors (haven't synced in >1 hour)
    scheduler.add_job(
        _enqueue_stale_connectors,
        "date",  # run once immediately
        id="startup_stale_sync",
        replace_existing=True,
    )

    scheduler.start()
    _scheduler = scheduler
    logger.info("scheduler_started")
    return scheduler


def stop_scheduler() -> None:
    """Shut down the running scheduler if active."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")
        _scheduler = None
