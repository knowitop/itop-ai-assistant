"""Vector index sweep: periodic incremental sync from registered VectorSource
instances (see `vector/source.py`, `vector_sources/registry.py`) into the
active `ChunkStore` (`vector/store.py`).

The sweep reads objects modified since the per-class cursor (with a
2×interval overlap), chunks them, embeds only changed chunks (hash-guard)
and upserts into the store. Cursor semantics: sources page independently and
may not guarantee ordering, so the cursor advances once per *completed class
pass* (max last_update seen), never per page; a crashed pass simply
re-reads, which the hash-guard makes cheap.

Backfill is the same code path with cursors reset, requested by a flag in
Redis (`vector/sync_state.py`) rather than by a flag in memory. A weekly
reconciliation pass deletes chunks of objects that vanished from their
source. Cross-replica exclusion is `VectorSyncState.sweep_lock()`.

The sweep is **infrastructure, not a business module**: it claims no trigger
route and writes no `RunJournal` entry — `register_vector_sweep` puts it under
the process scheduler and that is the whole of its relationship with the core.

This module is source-agnostic: it knows `VectorSource`/`VectorRecord`, never
`Ticket` or `ItopBundle` — those live in `vector_sources/tickets.py`.

`vector.enabled` and the embeddings section are re-read from the ConfigStore
snapshot on every tick, so enabling the feature at runtime needs no restart.
"""

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from itop_ai_assistant.config import EmbeddingsConfig, VectorConfig
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.vector.chunker import Chunk
from itop_ai_assistant.vector.embedder import EmbeddingsClient
from itop_ai_assistant.vector.source import VectorRecord, VectorSource
from itop_ai_assistant.vector.store import ChunkRecord, ChunkStore, FingerprintMismatchError, IndexMeta
from itop_ai_assistant.vector_sources.registry import build_vector_sources

logger = logging.getLogger(__name__)

_RECONCILE_BATCH = 200


@dataclass
class SweepReport:
    kind: str  # sweep / backfill
    status: str  # ok / error / skipped
    skip_reason: str | None = None
    objects_seen: int = 0
    chunks_embedded: int = 0
    chunks_deleted: int = 0
    errors: list[str] = field(default_factory=list)


SWEEP_TASK = "vector-sweep"


def register_vector_sweep(tasks: PeriodicTasks, deps: AppDeps) -> None:
    """Put the sweep under the process-wide scheduler.

    Infrastructure, not a business module: no `ModuleInfo`, no trigger route
    and no `RunJournal` entry — the sweep keeps its own `index_journal` in
    Postgres. What it needs from the core is pacing, and that is all it takes.
    """
    if not deps.vector_store.configured:
        logger.info("Vector store is not configured (database_url), the sweep will not run")
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
    `build_vector_sources` when omitted) — tests inject fakes here instead of
    mocking iTop/repository internals.
    """

    def __init__(self, deps: AppDeps, sources: Sequence[VectorSource] | None = None) -> None:
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
            return SweepReport(kind="sweep", status="skipped", skip_reason="database_url is not set")
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
            meta = await store.ensure_version(model, emb_cfg.dimension)
            if full:
                # Drops the pending-reindex flag along with the cursors — an
                # attempt that fails earlier leaves the request standing
                await self._deps.vector_sync.reset_cursors()
            sources = self._sources if self._sources is not None else build_vector_sources(self._deps, cfg)
            class_to_source = {obj_class: source for source in sources for obj_class in source.classes}
            active_sources = _dedupe(s for c in cfg.classes if (s := class_to_source.get(c)) is not None)
            for active_source in active_sources:
                await active_source.prepare()
            for obj_class in cfg.classes:
                source = class_to_source.get(obj_class)
                if source is None:
                    logger.warning(f"vector sweep: no source registered for class {obj_class!r} — skipping the class")
                    continue
                try:
                    await self._sweep_class(
                        obj_class,
                        source=source,
                        store=store,
                        meta=meta,
                        embedder=embedder,
                        cfg=cfg,
                        report=report,
                        started_at=started_at,
                    )
                except Exception as e:
                    # Class isolation: this class's cursor stays put, others proceed
                    logger.exception(f"vector sweep: class {obj_class} failed")
                    report.errors.append(f"{obj_class}: {e}")
            if not report.errors and await self._reconcile_due(cfg):
                await self._reconcile(store, class_to_source, cfg, report)
        except FingerprintMismatchError as e:
            logger.error(f"vector sweep: index rebuild required: {e}")
            report.errors.append(f"rebuild required: {e}")
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
                chunks_deleted=report.chunks_deleted,
                error="; ".join(report.errors) or None,
            )
            await embedder.aclose()
        return report

    async def _sweep_class(
        self,
        obj_class: str,
        *,
        source: VectorSource,
        store: ChunkStore,
        meta: IndexMeta,
        embedder: EmbeddingsClient,
        cfg: VectorConfig,
        report: SweepReport,
        started_at: datetime,
    ) -> None:
        class_cfg = cfg.classes[obj_class]
        profile = class_cfg.profile
        if not profile:
            logger.warning(f"vector sweep: no chunking profile for {obj_class} — skipping the class")
            return
        cursor = await self._deps.vector_sync.get_cursor(obj_class)
        # Overlap covers pages drifting while a previous pass ran; derived
        # from the interval instead of being one more config knob
        since = cursor - timedelta(seconds=2 * cfg.sweep_interval_seconds) if cursor else None
        max_seen = cursor
        page = 1
        while True:
            records = await source.find_modified_since(obj_class, since, page=page, page_size=cfg.sweep_page_size)
            # (record, chunks to embed, vanished chunk keys) — embedding is
            # batched per page: one embed() call for every changed chunk
            pending: list[tuple[VectorRecord, list[Chunk], list[tuple[str, int]]]] = []
            for record in records:
                report.objects_seen += 1
                if record.last_update and (max_seen is None or record.last_update > max_seen):
                    max_seen = record.last_update
                if class_cfg.index_values and record.index_value not in class_cfg.index_values:
                    # Left the indexable scope (e.g. reopened) — drop its chunks
                    report.chunks_deleted += await store.delete_object(obj_class, record.obj_id)
                    continue
                chunks = await source.chunk(
                    obj_class,
                    record,
                    profile,
                    max_chunk_tokens=cfg.max_chunk_tokens,
                    log_entries_per_chunk=cfg.log_entries_per_chunk,
                )
                stored = await store.get_chunk_hashes(obj_class, record.obj_id)
                changed = [c for c in chunks if stored.get((c.kind, c.n)) != c.content_hash]
                current_keys = {(c.kind, c.n) for c in chunks}
                vanished = [key for key in stored if key not in current_keys]
                if changed or vanished:
                    pending.append((record, changed, vanished))

            texts = [chunk.text for _, changed, _ in pending for chunk in changed]
            vectors = iter(await embedder.embed(texts) if texts else [])
            for record, changed, vanished in pending:
                chunk_records = [
                    ChunkRecord(
                        obj_class=obj_class,
                        obj_id=record.obj_id,
                        chunk_kind=chunk.kind,
                        chunk_n=chunk.n,
                        visibility=chunk.visibility,
                        status=record.index_value,
                        content_hash=chunk.content_hash,
                        embedding=next(vectors),
                        created_at=record.created_at or record.last_update or started_at,
                        org_id=record.org_id,
                        filters=record.filters,
                    )
                    for chunk in changed
                ]
                report.chunks_embedded += await store.upsert_chunks(chunk_records, model=meta.model, dim=meta.dim)
                report.chunks_deleted += await store.delete_chunks(obj_class, record.obj_id, vanished)

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
        self, store: ChunkStore, class_to_source: dict[str, VectorSource], cfg: VectorConfig, report: SweepReport
    ) -> None:
        """Delete chunks of objects that no longer exist at their source
        (deleted or archived — invisible to the incremental sweep)."""
        journal_id = await self._journal_start("reconcile")
        seen = deleted = 0
        status = "ok"
        error: str | None = None
        try:
            for obj_class in cfg.classes:
                source = class_to_source.get(obj_class)
                if source is None:
                    continue
                after = 0
                while True:
                    ids = await store.list_object_ids(obj_class, after=after, limit=_RECONCILE_BATCH)
                    if not ids:
                        break
                    seen += len(ids)
                    existing = await source.find_existing_ids(obj_class, ids)
                    for orphan in sorted(set(ids) - existing):
                        deleted += await store.delete_object(obj_class, orphan)
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


def _dedupe(sources: Iterable[VectorSource]) -> list[VectorSource]:
    """Unique sources by identity, order-preserving (a class→source map can
    map several classes onto the same source instance)."""
    seen: dict[int, VectorSource] = {}
    for source in sources:
        seen.setdefault(id(source), source)
    return list(seen.values())
