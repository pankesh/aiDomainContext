"""arq Redis worker for async sync tasks."""

from uuid import UUID

import structlog
from arq.connections import RedisSettings

from aidomaincontext.config import settings
from aidomaincontext.sync.worker import run_sync_job

logger = structlog.get_logger()


async def sync_connector_task(
    ctx: dict,
    connector_id: str,
    sync_type: str = "incremental",
) -> dict:
    """arq task: run a full sync for a single connector.

    Args:
        ctx: arq worker context (unused but required by arq).
        connector_id: UUID of the connector to sync (passed as string by arq).
        sync_type: "incremental" or "full".

    Returns:
        Summary dict with job id and status.
    """
    log = logger.bind(connector_id=connector_id, sync_type=sync_type)
    log.info("sync_connector_task_started")

    job = await run_sync_job(
        connector_id=UUID(connector_id),
        sync_type=sync_type,
    )

    log.info(
        "sync_connector_task_finished",
        sync_job_id=str(job.id),
        status=job.status,
        documents_synced=job.documents_synced,
        documents_failed=job.documents_failed,
    )

    return {
        "sync_job_id": str(job.id),
        "status": job.status,
        "documents_synced": job.documents_synced,
        "documents_failed": job.documents_failed,
    }


class WorkerSettings:
    """arq worker configuration.

    Start with:
        arq aidomaincontext.sync.arq_worker.WorkerSettings
    """

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [sync_connector_task]
    job_timeout = 600  # 10 minutes
