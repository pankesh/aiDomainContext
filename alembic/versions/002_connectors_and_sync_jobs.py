"""Add connectors and sync_jobs tables

Revision ID: 002
Revises: 001
Create Date: 2026-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("connector_type", sa.String(64), nullable=False),
        sa.Column("config_encrypted", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sync_cursor", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "connector_id",
            sa.UUID(),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sync_type", sa.String(32), nullable=False, server_default="incremental"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("documents_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_sync_jobs_connector_id", "sync_jobs", ["connector_id"])

    # Add FK from documents to connectors
    op.create_foreign_key(
        "fk_documents_connector_id",
        "documents",
        "connectors",
        ["connector_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_connector_id", "documents", type_="foreignkey")
    op.drop_table("sync_jobs")
    op.drop_table("connectors")
