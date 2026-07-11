"""Vector store foundation: pgvector extension + static tables.

The versioned chunk tables (vector_chunk_v{N}) are deliberately NOT created
here — their dimension comes from runtime config, so VectorIndex owns that
DDL (see src/vector/index.py).

Revision ID: 0001
Revises:
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Needs a role allowed to create extensions; true for the compose setup,
    # managed Postgres may require pre-provisioning the extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "vector_index_meta",
        sa.Column("version", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "vector_index_meta_one_active",
        "vector_index_meta",
        [sa.literal_column("(true)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "vector_sync_state",
        sa.Column("env", sa.Text(), primary_key=True),
        sa.Column("obj_class", sa.Text(), primary_key=True),
        sa.Column("cursor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "index_journal",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("objects_seen", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunks_embedded", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunks_deleted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("index_journal")
    op.drop_table("vector_sync_state")
    op.drop_index("vector_index_meta_one_active", table_name="vector_index_meta")
    op.drop_table("vector_index_meta")
    # The extension is left in place — other consumers may exist.
