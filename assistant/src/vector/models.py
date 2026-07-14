"""Vector store schema: static tables (owned by Alembic) and the versioned
chunk-table factory (owned by `VectorIndex` at runtime).

The chunk table stores embeddings + ids + filter metadata only — no raw text.
Anything shown to a user is re-fetched fresh from iTop by id
(docs/plans/vector-store.md §1).
"""

from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class VectorIndexMeta(Base):
    """One row per index version; the model fingerprint is the (model, dim) pair.

    A pgvector column has a fixed dimension, so a model/dimension change means
    a new versioned table — never mixed vectors in one table. At most one row
    is active (partial unique index).
    """

    __tablename__ = "vector_index_meta"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("vector_index_meta_one_active", text("(true)"), unique=True, postgresql_where=text("is_active")),
    )


class VectorSyncState(Base):
    """Per-class sweep cursor (max last_update seen in iTop)."""

    __tablename__ = "vector_sync_state"

    env: Mapped[str] = mapped_column(Text, primary_key=True)
    obj_class: Mapped[str] = mapped_column(Text, primary_key=True)
    cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IndexJournalEntry(Base):
    """History of indexing runs (sweep/backfill/reconcile) — written from Stage 2."""

    __tablename__ = "index_journal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    env: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # sweep / backfill / reconcile
    status: Mapped[str] = mapped_column(Text, nullable=False)  # running / ok / error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    objects_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunks_embedded: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunks_deleted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)


def chunk_table(version: int, dim: int) -> Table:
    """The versioned chunk table `vector_chunk_v{version}` with halfvec(dim).

    Built on a fresh MetaData so different (version, dim) pairs never clash.
    The single definition serves both DDL (table.create) and DML — no
    hand-written SQL strings (docs/plans/vector-store.md §2 schema).
    """
    name = f"vector_chunk_v{version}"
    return Table(
        name,
        MetaData(),
        Column("id", BigInteger, primary_key=True),
        Column("env", Text, nullable=False),
        Column("obj_class", Text, nullable=False),
        Column("obj_id", BigInteger, nullable=False),
        Column("chunk_kind", Text, nullable=False),  # profile / description / solution / log:public …
        Column("chunk_n", Integer, nullable=False),  # ordinal within kind
        Column("visibility", Text, nullable=False),  # public / internal
        Column("org_id", Text),  # rights pre-filter; NULL = global
        Column("status", Text, nullable=False),
        # Source-defined pre-filter keys, e.g. {"service_id": "5"} for
        # tickets or {"category": "..."} for a future KB source — short
        # scalar values only, never free text. Unlike `status`/`org_id`
        # (one concept, per-class vocabulary), these are genuinely different
        # concepts per source (Service vs. KB category vs. CI type), so they
        # don't share a single typed column identity.
        Column("filters", JSONB),
        Column("content_hash", Text, nullable=False),  # sha256 of the chunk's cleaned source text
        Column("embedding", HALFVEC(dim), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),  # object creation time
        Column("indexed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        UniqueConstraint("env", "obj_class", "obj_id", "chunk_kind", "chunk_n", name=f"{name}_chunk_key"),
        Index(
            f"{name}_emb_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
        Index(f"{name}_filter", "env", "obj_class", "status", "visibility"),
        Index(f"{name}_org", "org_id"),
        Index(f"{name}_obj", "env", "obj_class", "obj_id"),
        # jsonb_path_ops (not the default jsonb_ops): smaller and faster for
        # `@>` containment, which is the only operator this column is ever
        # queried with — no key-existence (`?`) lookups.
        Index(
            f"{name}_filters",
            "filters",
            postgresql_using="gin",
            postgresql_ops={"filters": "jsonb_path_ops"},
        ),
    )
