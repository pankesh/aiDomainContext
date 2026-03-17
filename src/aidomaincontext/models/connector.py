from typing import Optional

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aidomaincontext.models.base import Base, TimestampMixin, UUIDMixin


class Connector(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "connectors"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    config_encrypted: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sync_cursor: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
