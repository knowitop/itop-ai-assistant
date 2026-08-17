"""Vector index sweep: periodic incremental sync from registered VectorSource
instances (see `vector/ports/source.py`, `content_sources/registry.py`) into
the active `ChunkStore` (`vector/ports/store.py`).

The sweep reads objects modified since the per-class cursor (with a
2×interval overlap), chunks them, embeds only changed chunks (hash-guard)
and upserts into the store. Cursor semantics: sources page independently and
may not guarantee ordering, so the cursor advances once per *completed class
pass* (max last_update seen), never per page; a crashed pass simply
re-reads, which the hash-guard makes cheap.

Backfill is the same code path with cursors reset, requested by a flag in
Redis (`vector/state/sync_state.py`) rather than by a flag in memory. A weekly
reconciliation pass deletes chunks of objects that vanished from their
source. Cross-replica exclusion is `VectorSyncState.sweep_lock()`.

The sweep is **infrastructure, not a business module**: it claims no trigger
route and writes no `RunJournal` entry — `register_vector_sweep` puts it under
the process scheduler and that is the whole of its relationship with the core.

This module is source-agnostic: it knows `VectorSource`/`VectorRecord`, never
`Ticket` or a repository — those live in `content_sources/tickets.py`.

`vector.enabled` and the embeddings section are re-read from the ConfigStore
snapshot on every tick, so enabling the feature at runtime needs no restart.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from itop_ai_assistant.config import EmbeddingsConfig, FamilyConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.chunker import Chunk
from itop_ai_assistant.vector.ports.source import VectorRecord, VectorSource
from itop_ai_assistant.vector.ports.store import (
    ChunkDigest,
    ChunkMetadata,
    ChunkRecord,
    ChunkStore,
    FingerprintMismatchError,
    IndexMeta,
)
from itop_ai_assistant.vector.state.index_journal import IndexJournal
from itop_ai_assistant.vector.state.sync_state import VectorSyncState

logger = logging.getLogger(__name__)

_RECONCILE_BATCH = 200


class IndexerDeps(Protocol):
    """What the sweep needs from the container — declared here, by the
    consumer, rather than taken as `AppDeps` whole (ADR-019, `core.md`:
    "declare a port at the consumer", as `_AssignedDeps` and `ItopRepos` do).

    The five members below are the whole of it. `settings`, `itop_connection`,
    `state_manager`, `prompt_store` and the run `journal` are not here because
    the sweep never touches them: it is infrastructure with no run frame, and
    its own journal is `vector_journal`. `itop` is not here either — the
    sweep never calls it directly, only the composition root does, to build
    `vector_sources` (below) once and hand the sweep the result. `aclose()`
    is absent for the reason it is absent from every port — the pool belongs
    to the composition root.

    Read-only properties, never plain attributes, except `vector_sources`:
    an attribute is invariant, so e.g. `vector_store: ChunkStore` would
    reject an `AppDeps` whose field is typed more narrowly. `vector_sources`
    is a method instead, the same shape as `ItopAccess.for_principal`
    (`pipelines/ports.py`) — it is an operation (`VectorConfig` in, a fresh
    list out), not a stored value, so a method reads truer than a property
    that happens to return a callable. `AppDeps` satisfies all of this
    structurally and knows nothing about it — which is also what keeps
    `core/deps.py` out of this module entirely, cycle with the facade
    included.
    """

    @property
    def config_store(self) -> ConfigStore: ...

    def vector_sources(self, cfg: VectorConfig) -> Sequence[VectorSource[Any]]: ...

    @property
    def vector_store(self) -> ChunkStore: ...

    @property
    def vector_sync(self) -> VectorSyncState: ...

    @property
    def vector_journal(self) -> IndexJournal: ...


@dataclass
class SweepReport:
    kind: str  # sweep / backfill
    status: str  # ok / error / skipped
    skip_reason: str | None = None
    objects_seen: int = 0
    chunks_embedded: int = 0
    chunks_metadata_updated: int = 0
    chunks_deleted: int = 0
    errors: list[str] = field(default_factory=list)


SWEEP_TASK = "vector-sweep"


def register_vector_sweep(tasks: PeriodicTasks, deps: IndexerDeps) -> None:
    """Put the sweep under the process-wide scheduler.

    Infrastructure, not a business module: no `ModuleInfo`, no trigger route
    and no `RunJournal` entry — the sweep keeps its own `index_journal` in
    Redis. What it needs from the core is pacing, and that is all it takes.
    """
    if not deps.vector_store.configured:
        logger.info("Vector store is not configured (qdrant_url), the sweep will not run")
        return

    async def interval() -> float:
        return (await deps.config_store.get("vector", VectorConfig)).sweep_interval_seconds

    tasks.add(
        SWEEP_TASK,
        VectorIndexer(deps).tick,
        interval=interval,
        default_interval=VectorConfig().sweep_interval_seconds,
    )


class VectorIndexer:
    """One incremental sync pass. `sweep_once` is the whole thing — the
    scheduler (`pipelines/scheduler.py`) paces it, the CLI calls it directly.

    Nothing survives between passes in this object: a pending backfill is a
    row in `vector_sync_state`, so the request is honoured whatever process
    or replica ends up serving the next tick.

    `sources` overrides the registered `VectorSource`s (built by
    `deps.vector_sources(cfg)` when omitted) — tests inject fakes here
    instead of mocking iTop/repository internals.
    """

    def __init__(self, deps: IndexerDeps, sources: Sequence[VectorSource[Any]] | None = None) -> None:
        self._deps = deps
        self._sources = list(sources) if sources is not None else None

    async def tick(self) -> SweepReport:
        report = await self.sweep_once()
        if report.status == "error":
            logger.warning(f"vector sweep finished with errors: {'; '.join(report.errors)}")
        return report

    async def request_reindex(self) -> None:
        """Schedule a full backfill: the next sweep resets all cursors and
        runs as kind="backfill". No truncate — unchanged chunks are cheap
        thanks to the hash-guard, and reconciliation cleans orphans."""
        await self._deps.vector_sync.request_reindex()

    async def sweep_once(self) -> SweepReport:
        deps = self._deps
        if not deps.vector_store.configured:
            return SweepReport(kind="sweep", status="skipped", skip_reason="qdrant_url is not set")
        vector_cfg = await deps.config_store.get("vector", VectorConfig)
        if not vector_cfg.enabled:
            return SweepReport(kind="sweep", status="skipped", skip_reason="vector indexing is disabled")
        emb_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)
        model = emb_cfg.model
        if not emb_cfg.base_url or not model:
            return SweepReport(kind="sweep", status="skipped", skip_reason="embeddings endpoint is not configured")

        async with deps.vector_sync.sweep_lock() as locked:
            if not locked:
                return SweepReport(kind="sweep", status="skipped", skip_reason="another sweep holds the lock")
            return await self._sweep_locked(deps.vector_store, vector_cfg, emb_cfg, model)

    async def _sweep_locked(
        self, store: ChunkStore, cfg: VectorConfig, emb_cfg: EmbeddingsConfig, model: str
    ) -> SweepReport:
        started_at = datetime.now(UTC)
        full = await self._deps.vector_sync.reindex_pending()
        report = SweepReport(kind="backfill" if full else "sweep", status="ok")
        journal_id = await self._journal_start(report.kind)
        embedder = EmbeddingsClient(emb_cfg)
        try:
            if full:
                # Drops the pending-reindex flag along with the cursors — an
                # attempt that fails earlier leaves the request standing
                await self._deps.vector_sync.reset_cursors()
            sources = self._sources if self._sources is not None else self._deps.vector_sources(cfg)
            # A source may be prepared once here and again by reconcile below,
            # in the same pass — `prepared` keeps that to one call per source.
            prepared: set[int] = set()

            async def ensure_prepared(source: VectorSource[Any]) -> None:
                if id(source) not in prepared:
                    await source.prepare()
                    prepared.add(id(source))

            for source in sources:
                family = source.name
                family_cfg = cfg.families.get(family)
                if family_cfg is None:
                    logger.warning(f"vector sweep: no config entry for family {family!r} — skipping")
                    continue
                if not source.classes:
                    continue
                # Only a family with its *own* interval gets paced against
                # its last real pass — the scheduler's own tick cadence
                # already enforces the system-wide interval, so gating an
                # un-overridden family here too would double up on the same
                # value and could skip an out-of-band tick ("Index now", or
                # the scheduler firing a hair early) that arrives before a
                # full system interval has elapsed since the last one.
                if not full and family_cfg.sweep_interval_seconds is not None:
                    last_swept = await self._deps.vector_sync.get_family_swept(family)
                    if last_swept is not None and started_at - last_swept < timedelta(
                        seconds=family_cfg.sweep_interval_seconds
                    ):
                        continue
                try:
                    await ensure_prepared(source)
                    meta = await store.ensure_version(
                        family, model, emb_cfg.dimension, filter_keys=source.indexed_filter_keys
                    )
                except FingerprintMismatchError as e:
                    logger.error(f"vector sweep: index rebuild required for family {family!r}: {e}")
                    report.errors.append(f"rebuild required: {e}")
                    continue
                except Exception as e:
                    # Family isolation, same as the per-class catch below:
                    # this family's classes stay untouched, others proceed.
                    logger.exception(f"vector sweep: family {family!r} failed")
                    report.errors.append(f"{family}: {e}")
                    continue
                for obj_class in source.classes:
                    class_cfg = family_cfg.classes.get(obj_class)
                    if class_cfg is None:
                        logger.warning(
                            f"vector sweep: no config for class {obj_class!r} under family {family!r} — skipping"
                        )
                        continue
                    try:
                        await self._sweep_class(
                            obj_class,
                            family=family,
                            source=source,
                            store=store,
                            meta=meta,
                            embedder=embedder,
                            cfg=cfg,
                            family_cfg=family_cfg,
                            class_cfg=class_cfg,
                            report=report,
                            started_at=started_at,
                        )
                    except Exception as e:
                        # Class isolation: this class's cursor stays put, others proceed
                        logger.exception(f"vector sweep: class {obj_class} failed")
                        report.errors.append(f"{obj_class}: {e}")
                await self._deps.vector_sync.set_family_swept(family, started_at)
            if not report.errors and await self._reconcile_due(cfg):
                await self._reconcile(store, sources, cfg, report, ensure_prepared)
        except Exception as e:
            logger.exception("vector sweep failed")
            report.errors.append(str(e))
        finally:
            if report.errors:
                report.status = "error"
            await self._journal_finish(
                journal_id,
                status=report.status,
                objects_seen=report.objects_seen,
                chunks_embedded=report.chunks_embedded,
                chunks_metadata_updated=report.chunks_metadata_updated,
                chunks_deleted=report.chunks_deleted,
                error="; ".join(report.errors) or None,
            )
            await embedder.aclose()
        return report

    async def _sweep_class(
        self,
        obj_class: str,
        *,
        family: str,
        source: VectorSource[Any],
        store: ChunkStore,
        meta: IndexMeta,
        embedder: EmbeddingsClient,
        cfg: VectorConfig,
        family_cfg: FamilyConfig,
        class_cfg: VectorClassConfig,
        report: SweepReport,
        started_at: datetime,
    ) -> None:
        if not class_cfg.chunks:
            logger.warning(f"vector sweep: no chunk fragments configured for {obj_class} — skipping the class")
            return
        interval = family_cfg.sweep_interval_seconds or cfg.sweep_interval_seconds
        log_entries_per_chunk = family_cfg.log_entries_per_chunk or cfg.log_entries_per_chunk
        cursor = await self._deps.vector_sync.get_cursor(obj_class)
        # Overlap covers pages drifting while a previous pass ran; derived
        # from the family's own interval instead of being one more config knob
        since = cursor - timedelta(seconds=2 * interval) if cursor else None
        max_seen = cursor
        page = 1
        while True:
            records = await source.find_modified_since(obj_class, since, page=page, page_size=cfg.sweep_page_size)
            # (record, chunks to embed [with their metadata built], metadata-only
            # rewrites, vanished chunk keys) — embedding is batched per page: one
            # embed() call for every changed chunk
            pending: list[
                tuple[VectorRecord[Any], list[tuple[Chunk, ChunkMetadata]], list[ChunkMetadata], list[tuple[str, int]]]
            ] = []
            for record in records:
                report.objects_seen += 1
                if record.updated_at and (max_seen is None or record.updated_at > max_seen):
                    max_seen = record.updated_at
                if class_cfg.index_values and record.index_value not in class_cfg.index_values:
                    # Left the indexable scope (e.g. reopened) — drop its chunks
                    report.chunks_deleted += await store.delete_object(family, obj_class, record.obj_id)
                    continue
                chunks = await source.chunk(
                    obj_class,
                    record,
                    class_cfg,
                    max_chunk_tokens=cfg.max_chunk_tokens,
                    log_entries_per_chunk=log_entries_per_chunk,
                )
                stored = await store.get_chunk_digests(family, obj_class, record.obj_id)
                # Object-level once, chunk-level per chunk — `stored` is read
                # first because the creation date may have to be inherited
                # from it (`_creation_date`).
                object_meta = _object_metadata(obj_class, record, stored, started_at)
                # A chunk lands in exactly one bucket: content changed wins over
                # metadata-only, since upsert_chunks rewrites the whole payload
                # anyway (including a fresh meta_hash).
                changed: list[tuple[Chunk, ChunkMetadata]] = []
                stale_meta: list[ChunkMetadata] = []
                for chunk in chunks:
                    chunk_meta = object_meta.for_chunk(chunk)
                    digest = stored.get((chunk.kind, chunk.n))
                    if digest is None or digest.content_hash != chunk.content_hash:
                        changed.append((chunk, chunk_meta))
                    elif digest.meta_hash != chunk_meta.meta_hash:
                        stale_meta.append(chunk_meta)
                current_keys = {(c.kind, c.n) for c in chunks}
                vanished = [key for key in stored if key not in current_keys]
                if changed or stale_meta or vanished:
                    pending.append((record, changed, stale_meta, vanished))

            texts = [chunk.text for _, changed, _, _ in pending for chunk, _ in changed]
            vectors = iter(await embedder.embed(texts) if texts else [])
            for record, changed, stale_meta, vanished in pending:
                chunk_records = [ChunkRecord(meta=chunk_meta, embedding=next(vectors)) for _, chunk_meta in changed]
                report.chunks_embedded += await store.upsert_chunks(
                    chunk_records, family=family, model=meta.model, dim=meta.dim
                )
                report.chunks_metadata_updated += await store.update_chunk_metadata(stale_meta, family=family)
                report.chunks_deleted += await store.delete_chunks(family, obj_class, record.obj_id, vanished)

            if len(records) < cfg.sweep_page_size:
                break
            page += 1
            await asyncio.sleep(cfg.sweep_throttle_seconds)

        if max_seen is not None and max_seen != cursor:
            await self._deps.vector_sync.set_cursor(obj_class, max_seen)

    async def _reconcile_due(self, cfg: VectorConfig) -> bool:
        last = await self._deps.vector_sync.get_reconcile()
        return last is None or datetime.now(UTC) - last >= timedelta(days=cfg.reconcile_interval_days)

    async def _reconcile(
        self,
        store: ChunkStore,
        sources: Sequence[VectorSource[Any]],
        cfg: VectorConfig,
        report: SweepReport,
        ensure_prepared: Callable[[VectorSource[Any]], Awaitable[None]],
    ) -> None:
        """Delete chunks of objects that no longer exist at their source
        (deleted or archived — invisible to the incremental sweep).

        Runs on its own cadence (`reconcile_interval_days`), not the
        per-family pacing the sweep above applies — every configured class of
        every source is reconciled every time it is due."""
        journal_id = await self._journal_start("reconcile")
        seen = deleted = 0
        status = "ok"
        error: str | None = None
        try:
            for source in sources:
                family = source.name
                if family not in cfg.families or not source.classes:
                    continue
                await ensure_prepared(source)
                for obj_class in source.classes:
                    after = 0
                    while True:
                        ids = await store.list_object_ids(family, obj_class, after=after, limit=_RECONCILE_BATCH)
                        if not ids:
                            break
                        seen += len(ids)
                        existing = await source.find_existing_ids(obj_class, ids)
                        for orphan in sorted(set(ids) - existing):
                            deleted += await store.delete_object(family, obj_class, orphan)
                        after = ids[-1]
                        await asyncio.sleep(cfg.sweep_throttle_seconds)
            await self._deps.vector_sync.set_reconcile(datetime.now(UTC))
        except Exception as e:
            logger.exception("vector reconciliation failed")
            status = "error"
            error = str(e)
            report.errors.append(f"reconcile: {e}")
        finally:
            await self._journal_finish(
                journal_id, status=status, objects_seen=seen, chunks_deleted=deleted, error=error
            )

    # Journal writes are observability, not correctness — never fail the sweep

    async def _journal_start(self, kind: str) -> str | None:
        try:
            return await self._deps.vector_journal.start(kind)
        except Exception as e:
            logger.warning(f"index journal start failed (non-fatal): {e}")
            return None

    async def _journal_finish(self, journal_id: str | None, **kwargs) -> None:
        if journal_id is None:
            return
        try:
            await self._deps.vector_journal.finish(journal_id, **kwargs)
        except Exception as e:
            logger.warning(f"index journal finish failed (non-fatal): {e}")


@dataclass(frozen=True)
class _ObjectMetadata:
    """The half of `ChunkMetadata` that describes the object, not the chunk.

    Built once per record rather than once per chunk — which is not only
    cheaper but the thing that makes freezing `created_at` possible at all:
    the question "when did this object come into being" has one answer, and
    every chunk of the object has to land on it. Computing it per chunk is
    how the chunks of one object drifted apart in the first place (TASK-020).
    """

    obj_class: str
    obj_id: int
    filters: dict[str, str]
    created_at: datetime
    updated_at: datetime | None

    def for_chunk(self, chunk: Chunk) -> ChunkMetadata:
        return ChunkMetadata(
            obj_class=self.obj_class,
            obj_id=self.obj_id,
            chunk_kind=chunk.kind,
            chunk_n=chunk.n,
            visibility=chunk.visibility,
            content_hash=chunk.content_hash,
            created_at=self.created_at,
            filters=self.filters,
            # No fallback, unlike `created_at`: a source without a
            # modification date opts out of the `updated` window, which is the
            # honest answer — there is nothing to freeze, since the field is
            # supposed to move.
            updated_at=self.updated_at,
        )


def _object_metadata(
    obj_class: str,
    record: VectorRecord[Any],
    stored: dict[tuple[str, int], ChunkDigest],
    started_at: datetime,
) -> _ObjectMetadata:
    filters = dict(record.filters or {})
    filters["status"] = record.index_value
    if record.org_id is not None:
        filters["org_id"] = record.org_id
    return _ObjectMetadata(
        obj_class=obj_class,
        obj_id=record.obj_id,
        filters=filters,
        created_at=_creation_date(record, stored, started_at),
        updated_at=record.updated_at,
    )


def _creation_date(
    record: VectorRecord[Any], stored: dict[tuple[str, int], ChunkDigest], started_at: datetime
) -> datetime:
    """When the object came into being, as the index will remember it.

    The source's own date if it has one. Otherwise whatever the object's
    already-indexed chunks say — that is what freezes the value: the last two
    fallbacks fire once, at first indexing, and every pass afterwards
    inherits. Without that step a fresh `started_at` would enter the payload
    on every rewrite, and since rewrites are per-chunk, the chunks of one
    object would end up claiming different creation dates — visible as an
    object matching a `created` window through some of its chunks and not
    others (TASK-020).

    `min` over the stored values, not "this chunk's own": a chunk added later
    (a new log window) has nothing stored and would otherwise take the current
    clock while its siblings keep the old one. Earliest-known also cannot
    creep forward from pass to pass.

    Stored beats `record.updated_at` deliberately — a modification date moves
    with every edit, so as a fallback it is no better than the sweep's clock.
    """
    if record.created_at is not None:
        return record.created_at
    indexed = [digest.created_at for digest in stored.values() if digest.created_at is not None]
    if indexed:
        return min(indexed)
    return record.updated_at or started_at
