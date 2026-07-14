"""VectorIndex — the single place that knows SQL/pgvector.

Chunker, indexer and retriever (later stages) speak to storage only through
this interface, so the storage layer stays swappable and testable in
isolation (docs/plans/vector-store.md §3).

Schema ownership note (deviation from the "all DDL through Alembic" rule):
the versioned chunk tables `vector_chunk_v{N}` are created here at runtime,
not in a migration — their dimension comes from the runtime-editable
embeddings config and the table name from `vector_index_meta`, neither of
which is known at migration-authoring time. The DDL is still expressed once,
as the SQLAlchemy `Table` factory in models.py (no SQL strings), and the
static tables remain Alembic-owned. A model/dimension change never mutates
an existing table: it requires a new version (v{N+1} rebuild — Stage 2+),
which `FingerprintMismatchError` enforces.
"""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, desc, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from vector.db import VectorDb
from vector.models import IndexJournalEntry, VectorIndexMeta, VectorSyncState, chunk_table

# vector_sync_state row that stores the last reconciliation time instead of a
# per-class sweep cursor; filtered out of list_cursors()
RECONCILE_SENTINEL = "__reconcile__"


@dataclass(frozen=True)
class IndexMeta:
    """The active index version and its model fingerprint (model, dim)."""

    version: int
    model: str
    dim: int


@dataclass(frozen=True)
class IndexStats:
    version: int
    rows: int
    size_bytes: int


@dataclass(frozen=True)
class ChunkRecord:
    """One embedded chunk of an iTop object — ids and filter metadata, no text."""

    obj_class: str
    obj_id: int
    chunk_kind: str  # profile / description / solution / log:public …
    chunk_n: int
    visibility: str  # public / internal
    status: str
    content_hash: str
    embedding: list[float]
    created_at: datetime  # object creation time (time-window KNN later)
    org_id: str | None = None
    filters: dict[str, str] | None = None  # source-defined pre-filter keys, see vector/models.py


@dataclass(frozen=True)
class SearchHit:
    obj_id: int
    score: float


class FingerprintMismatchError(Exception):
    """The active index was built with a different model/dim — rebuild required."""


class VectorIndex:
    def __init__(self, db: VectorDb, env: str = "main") -> None:
        self._db = db
        self._env = env

    async def active_meta(self) -> IndexMeta | None:
        async with self._db.connect() as conn:
            return await self._read_active(conn)

    async def ensure_version(self, model: str, dim: int) -> IndexMeta:
        """Return the active version, creating v1 (meta row + chunk table)
        on first use. Raises FingerprintMismatchError when an active version
        exists with a different model/dim — this stage never auto-rebuilds.
        """
        async with self._db.engine.begin() as conn:
            meta = await self._read_active(conn, for_update=True)
            if meta is not None:
                self._check_fingerprint(meta, model, dim)
                return meta
            max_version = (await conn.execute(select(func.coalesce(func.max(VectorIndexMeta.version), 0)))).scalar_one()
            version = max_version + 1
            table = chunk_table(version, dim)
            await conn.run_sync(lambda sync_conn: table.create(sync_conn))
            await conn.execute(insert(VectorIndexMeta).values(version=version, model=model, dim=dim, is_active=True))
            return IndexMeta(version=version, model=model, dim=dim)

    async def upsert_chunks(self, chunks: list[ChunkRecord], *, model: str, dim: int) -> int:
        """Idempotent insert-or-update by (env, obj_class, obj_id, chunk_kind, chunk_n)."""
        if not chunks:
            return 0
        async with self._db.engine.begin() as conn:
            meta = self._require_active(await self._read_active(conn))
            self._check_fingerprint(meta, model, dim)
            table = chunk_table(meta.version, meta.dim)
            stmt = pg_insert(table).values(
                [
                    {
                        "env": self._env,
                        "obj_class": c.obj_class,
                        "obj_id": c.obj_id,
                        "chunk_kind": c.chunk_kind,
                        "chunk_n": c.chunk_n,
                        "visibility": c.visibility,
                        "status": c.status,
                        "org_id": c.org_id,
                        "filters": c.filters,
                        "content_hash": c.content_hash,
                        "embedding": c.embedding,
                        "created_at": c.created_at,
                    }
                    for c in chunks
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["env", "obj_class", "obj_id", "chunk_kind", "chunk_n"],
                set_={
                    "visibility": stmt.excluded.visibility,
                    "status": stmt.excluded.status,
                    "org_id": stmt.excluded.org_id,
                    "filters": stmt.excluded.filters,
                    "content_hash": stmt.excluded.content_hash,
                    "embedding": stmt.excluded.embedding,
                    "created_at": stmt.excluded.created_at,
                    "indexed_at": func.now(),
                },
            )
            result = await conn.execute(stmt)
            return result.rowcount

    async def delete_object(self, obj_class: str, obj_id: int) -> int:
        """Delete every chunk of one object (env-scoped). Returns rows deleted."""
        async with self._db.engine.begin() as conn:
            meta = self._require_active(await self._read_active(conn))
            table = chunk_table(meta.version, meta.dim)
            result = await conn.execute(
                delete(table).where(
                    table.c.env == self._env,
                    table.c.obj_class == obj_class,
                    table.c.obj_id == obj_id,
                )
            )
            return result.rowcount

    async def search(
        self,
        embedding: list[float],
        *,
        classes: list[str],
        statuses: list[str],
        visibilities: list[str],
        allowed_orgs: list[str] | None = None,
        exclude_obj_id: int | None = None,
        limit: int = 30,
    ) -> list[SearchHit]:
        """Filtered KNN aggregated to objects: max cosine similarity over an
        object's chunks (a ticket matching on both description and solution
        must not count twice). `allowed_orgs=None` means unrestricted.
        Returns [] when no index version exists yet.
        """
        meta = await self.active_meta()
        if meta is None:
            return []
        table = chunk_table(meta.version, meta.dim)
        score = func.max(1 - table.c.embedding.cosine_distance(embedding)).label("score")
        stmt = (
            select(table.c.obj_id, score)
            .where(
                table.c.env == self._env,
                table.c.obj_class.in_(classes),
                table.c.status.in_(statuses),
                table.c.visibility.in_(visibilities),
            )
            .group_by(table.c.obj_id)
            .order_by(desc("score"))
            .limit(limit)
        )
        if allowed_orgs is not None:
            stmt = stmt.where(table.c.org_id.in_(allowed_orgs))
        if exclude_obj_id is not None:
            stmt = stmt.where(table.c.obj_id != exclude_obj_id)
        async with self._db.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [SearchHit(obj_id=row.obj_id, score=float(row.score)) for row in rows]

    async def stats(self) -> IndexStats | None:
        meta = await self.active_meta()
        if meta is None:
            return None
        table = chunk_table(meta.version, meta.dim)
        async with self._db.connect() as conn:
            rows = (await conn.execute(select(func.count()).select_from(table))).scalar_one()
            size = (await conn.execute(select(func.pg_total_relation_size(table.name)))).scalar_one()
        return IndexStats(version=meta.version, rows=rows, size_bytes=size)

    async def get_chunk_hashes(self, obj_class: str, obj_id: int) -> dict[tuple[str, int], str]:
        """Stored content hashes of one object, keyed by (chunk_kind, chunk_n)."""
        meta = await self.active_meta()
        if meta is None:
            return {}
        table = chunk_table(meta.version, meta.dim)
        async with self._db.connect() as conn:
            rows = (
                await conn.execute(
                    select(table.c.chunk_kind, table.c.chunk_n, table.c.content_hash).where(
                        table.c.env == self._env,
                        table.c.obj_class == obj_class,
                        table.c.obj_id == obj_id,
                    )
                )
            ).all()
        return {(row.chunk_kind, row.chunk_n): row.content_hash for row in rows}

    async def delete_chunks(self, obj_class: str, obj_id: int, keys: list[tuple[str, int]]) -> int:
        """Delete specific chunks of one object (vanished kinds/ordinals)."""
        if not keys:
            return 0
        async with self._db.engine.begin() as conn:
            meta = self._require_active(await self._read_active(conn))
            table = chunk_table(meta.version, meta.dim)
            result = await conn.execute(
                delete(table).where(
                    table.c.env == self._env,
                    table.c.obj_class == obj_class,
                    table.c.obj_id == obj_id,
                    tuple_(table.c.chunk_kind, table.c.chunk_n).in_(keys),
                )
            )
            return result.rowcount

    async def list_object_ids(self, obj_class: str, after: int = 0, limit: int = 1000) -> list[int]:
        """Distinct indexed obj_ids > `after`, ascending — keyset pagination
        for the reconciliation walk."""
        meta = await self.active_meta()
        if meta is None:
            return []
        table = chunk_table(meta.version, meta.dim)
        stmt = (
            select(table.c.obj_id)
            .distinct()
            .where(
                table.c.env == self._env,
                table.c.obj_class == obj_class,
                table.c.obj_id > after,
            )
            .order_by(table.c.obj_id)
            .limit(limit)
        )
        async with self._db.connect() as conn:
            return list((await conn.execute(stmt)).scalars())

    async def get_cursor(self, obj_class: str) -> datetime | None:
        async with self._db.connect() as conn:
            row = (
                await conn.execute(
                    select(VectorSyncState.cursor).where(
                        VectorSyncState.env == self._env,
                        VectorSyncState.obj_class == obj_class,
                    )
                )
            ).one_or_none()
        return row.cursor if row else None

    async def set_cursor(self, obj_class: str, cursor: datetime) -> None:
        stmt = pg_insert(VectorSyncState).values(env=self._env, obj_class=obj_class, cursor=cursor)
        stmt = stmt.on_conflict_do_update(
            index_elements=["env", "obj_class"],
            set_={"cursor": stmt.excluded.cursor, "updated_at": func.now()},
        )
        async with self._db.engine.begin() as conn:
            await conn.execute(stmt)

    async def list_cursors(self) -> dict[str, datetime | None]:
        """Per-class sweep cursors (the reconcile sentinel is excluded)."""
        async with self._db.connect() as conn:
            rows = (
                await conn.execute(
                    select(VectorSyncState.obj_class, VectorSyncState.cursor).where(
                        VectorSyncState.env == self._env,
                        VectorSyncState.obj_class != RECONCILE_SENTINEL,
                    )
                )
            ).all()
        return {row.obj_class: row.cursor for row in rows}

    async def reset_cursors(self) -> None:
        """Drop all sweep cursors (and the reconcile mark) — the next sweep
        becomes a full backfill."""
        async with self._db.engine.begin() as conn:
            await conn.execute(delete(VectorSyncState).where(VectorSyncState.env == self._env))

    async def journal_start(self, kind: str) -> int:
        async with self._db.engine.begin() as conn:
            return (
                await conn.execute(
                    insert(IndexJournalEntry)
                    .values(env=self._env, kind=kind, status="running")
                    .returning(IndexJournalEntry.id)
                )
            ).scalar_one()

    async def journal_finish(
        self,
        entry_id: int,
        *,
        status: str,
        objects_seen: int = 0,
        chunks_embedded: int = 0,
        chunks_deleted: int = 0,
        error: str | None = None,
    ) -> None:
        async with self._db.engine.begin() as conn:
            await conn.execute(
                update(IndexJournalEntry)
                .where(IndexJournalEntry.id == entry_id)
                .values(
                    status=status,
                    finished_at=func.now(),
                    objects_seen=objects_seen,
                    chunks_embedded=chunks_embedded,
                    chunks_deleted=chunks_deleted,
                    error=error,
                )
            )

    async def journal_recent(self, limit: int = 10) -> list[dict]:
        async with self._db.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        IndexJournalEntry.id,
                        IndexJournalEntry.kind,
                        IndexJournalEntry.status,
                        IndexJournalEntry.started_at,
                        IndexJournalEntry.finished_at,
                        IndexJournalEntry.objects_seen,
                        IndexJournalEntry.chunks_embedded,
                        IndexJournalEntry.chunks_deleted,
                        IndexJournalEntry.error,
                    )
                    .where(IndexJournalEntry.env == self._env)
                    .order_by(desc(IndexJournalEntry.id))
                    .limit(limit)
                )
            ).mappings()
            return [dict(row) for row in rows]

    @asynccontextmanager
    async def try_advisory_lock(self) -> AsyncIterator[bool]:
        """Session-level pg advisory lock guarding the sweep across replicas.

        Holds one dedicated connection for the whole context — a session lock
        lives and dies with its connection, so the connection must not return
        to the pool mid-tick. Yields False (without waiting) when another
        session holds the lock.
        """
        key = self._advisory_lock_key()
        async with self._db.connect() as conn:
            locked = (await conn.execute(select(func.pg_try_advisory_lock(key)))).scalar_one()
            try:
                yield locked
            finally:
                if locked:
                    await conn.execute(select(func.pg_advisory_unlock(key)))

    def _advisory_lock_key(self) -> int:
        digest = hashlib.sha256(f"vector_sweep:{self._env}".encode()).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    async def _read_active(self, conn: AsyncConnection, for_update: bool = False) -> IndexMeta | None:
        stmt = select(VectorIndexMeta.version, VectorIndexMeta.model, VectorIndexMeta.dim).where(
            VectorIndexMeta.is_active
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await conn.execute(stmt)).one_or_none()
        return IndexMeta(version=row.version, model=row.model, dim=row.dim) if row else None

    @staticmethod
    def _require_active(meta: IndexMeta | None) -> IndexMeta:
        if meta is None:
            raise FingerprintMismatchError("No active index version — call ensure_version first")
        return meta

    @staticmethod
    def _check_fingerprint(meta: IndexMeta, model: str, dim: int) -> None:
        if (meta.model, meta.dim) != (model, dim):
            raise FingerprintMismatchError(
                f"Active index v{meta.version} was built with ({meta.model!r}, dim={meta.dim}); "
                f"current config is ({model!r}, dim={dim}) — rebuild the index before writing"
            )
