import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aidomaincontext.models.base import Base, TimestampMixin, UUIDMixin


class SyncJob(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sync_jobs"

    connector_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False
    )
    sync_type: Mapped[str] = mapped_column(String(32), nullable=False, default="incremental")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    documents_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    documents_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
