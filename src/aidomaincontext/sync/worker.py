"""Async sync job runner — fetches documents from a connector and ingests them."""

from datetime import datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import select

from aidomaincontext.connectors.base import get_connector
from aidomaincontext.ingestion.pipeline import ingest_document
from aidomaincontext.security import decrypt_config
from aidomaincontext.models.connector import Connector
from aidomaincontext.models.database import async_session
from aidomaincontext.models.sync_job import SyncJob

logger = structlog.get_logger()


async def run_sync_job(
    connector_id: UUID,
    sync_type: str = "incremental",
) -> SyncJob:
    """Execute a full sync for a single connector.

    Creates a SyncJob record, iterates through documents from the connector,
    ingests each one, and updates the sync cursor on the connector.
    """
    async with async_session() as session:
        # Load the connector
        result = await session.execute(
            select(Connector).where(Connector.id == connector_id)
        )
        connector = result.scalar_one_or_none()
        if connector is None:
            raise ValueError(f"Connector {connector_id} not found")

        log = logger.bind(
            connector_id=str(connector_id),
            connector_type=connector.connector_type,
            sync_type=sync_type,
        )

        # Create the SyncJob record
        job = SyncJob(
            connector_id=connector_id,
            sync_type=sync_type,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        await session.flush()
        log = log.bind(sync_job_id=str(job.id))
        log.info("sync_job_started")

        documents_synced = 0
        documents_failed = 0

        try:
            impl = get_connector(connector.connector_type)
            cursor = connector.sync_cursor if sync_type == "incremental" else None

            async for doc_data, new_cursor in impl.fetch_documents(
                decrypt_config(connector.config_encrypted), cursor
            ):
                try:
                    await ingest_document(
                        session,
                        doc_data,
                        connector_id=connector_id,
                    )
                    documents_synced += 1
                    connector.sync_cursor = new_cursor
                    log.debug(
                        "document_synced",
                        source_id=doc_data.source_id,
                        documents_synced=documents_synced,
                    )
                except Exception:
                    documents_failed += 1
                    log.exception(
                        "document_ingest_failed",
                        source_id=doc_data.source_id,
                        documents_failed=documents_failed,
                    )

            job.status = "completed"
            job.documents_synced = documents_synced
            job.documents_failed = documents_failed
            log.info(
                "sync_job_completed",
                documents_synced=documents_synced,
                documents_failed=documents_failed,
            )

        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            job.documents_synced = documents_synced
            job.documents_failed = documents_failed
            log.exception(
                "sync_job_failed",
                error=str(exc),
                documents_synced=documents_synced,
                documents_failed=documents_failed,
            )

        finally:
            job.finished_at = datetime.now(timezone.utc)
            await session.commit()

    return job
