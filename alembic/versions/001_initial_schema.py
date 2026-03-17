"""Initial schema - documents and chunks tables

Revision ID: 001
Revises: None
Create Date: 2026-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("connector_id", sa.UUID(), nullable=True),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("author", sa.String(256), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default='{"is_public": true}'),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("connector_id", "source_id", name="uq_connector_source"),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Create indexes
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.execute("""
        CREATE INDEX ix_chunks_embedding ON chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)
    op.execute("""
        CREATE INDEX ix_chunks_content_fts ON chunks
        USING gin (to_tsvector('english', content))
    """)


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("documents")
