"""Vector index sweep: periodic incremental sync from registered VectorSource
instances (see `vector/ports/source.py`, `content_sources/registry.py`) into
the active `ChunkStore` (`vector/ports/store.py`).

The sweep reads objects modified since the per-class cursor (with a
2×interval overlap), chunks them, embeds only changed chunks (hash-guard)
and upserts into the store. Cursor semantics: sources page independently and
may not guarantee ordering, so the cursor advances once per *completed class
pass* (max last_update seen), never per page; a crashed pass simply
re-reads, which the hash-guard makes cheap.

The interval is counted from the last pass rather than from the start of the
process: a loop's cadence lives in one process, and a service restarted before
the interval was up used to sweep again immediately, however often it was
restarted. The marker (`VectorSyncState.get_swept`) is what a timer tick is
paced against; a requested backfill, "Index now" and the CLI go through
regardless.

Backfill is the same code path with cursors reset, requested by a flag in
Redis (`vector/state/sync_state.py`) rather than by a flag in memory. A weekly
reconciliation pass deletes chunks of objects that vanished from their
source. Cross-replica exclusion is `VectorSyncState.sweep_lock()`.

A changed embeddings model is the same code path too. The store hands back
the version it wants filled (`ensure_version`), and a version that is not the
active one means the model changed and this pass is filling its replacement
from empty: the class walk ignores its cursor, and the family is switched
over once a pass has got through all of its classes with no errors. Nothing
about that lives in Redis — abandoning a rebuild is a config edit and costs
no cleanup.

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
from typing import Any

from itop_ai_assistant.config import EmbeddingsConfig
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.chunker import Chunk
from itop_ai_assistant.vector.config import FamilyConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.vector.domain import ChunkSyncState, classify_chunk, creation_date, left_indexable_scope
from itop_ai_assistant.vector.ports.source import ChunkPlan, VectorRecord, VectorSource
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
# `warnings` is per-object, so a pass over a large class can produce one entry
# per record; `errors` is per class and per family and bounds itself.
_MAX_WARNINGS = 20


@dataclass
class SweepReport:
    kind: str  # sweep / backfill
    status: str  # ok / error / skipped
    skip_reason: str | None = None
    objects_seen: int = 0
    objects_skipped: int = 0
    chunks_embedded: int = 0
    chunks_metadata_updated: int = 0
    chunks_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    # Not errors: the pass did what it should and the object is the problem.
    # Only `errors` decides `status` and gates reconciliation, so a skipped
    # object must never land here — see `_sweep_locked`.
    warnings: list[str] = field(default_factory=list)

    def warning_text(self) -> str | None:
        """The warnings as one journal field, capped — an installation that
        inlines attachments into every article would otherwise write the whole
        class into one Redis value."""
        if not self.warnings:
            return None
        head = "; ".join(self.warnings[:_MAX_WARNINGS])
        rest = len(self.warnings) - _MAX_WARNINGS
        return f"{head}; and {rest} more" if rest > 0 else head


# (record, chunks to embed [with their metadata built], metadata-only
# rewrites, vanished chunk keys) — one page's worth of work per object.
type _Pending = tuple[VectorRecord[Any], list[tuple[Chunk, ChunkMetadata]], list[ChunkMetadata], list[tuple[str, int]]]


def _embed_breakdown(obj_class: str, pending: list[_Pending], limit: int = 5) -> str:
    """Which objects a page's single embed call is paying for, heaviest first.

    Embedding is batched per page, so neither the call nor a failure inside it
    names an object; this is what turns "class FAQ failed" into an id to open
    in iTop. `FAQ::42` is that object's iTop key — the `id` in its URL, not a
    ticket ref and not an index-local number.
    """
    sizes = sorted(
        (
            (record.obj_id, len(changed), sum(len(chunk.text) for chunk, _ in changed))
            for record, changed, _, _ in pending
            if changed
        ),
        key=lambda item: item[2],
        reverse=True,
    )
    head = ", ".join(f"{obj_class}::{obj_id} ({count} chunks, {chars} chars)" for obj_id, count, chars in sizes[:limit])
    return head + (f" and {len(sizes) - limit} more" if len(sizes) > limit else "")


SWEEP_TASK = "vector-sweep"


def register_vector_sweep(
    tasks: PeriodicTasks,
    config_store: ConfigStore,
    vector_sources: Callable[[VectorConfig], Sequence[VectorSource[Any]]],
    vector_store: ChunkStore,
    vector_sync: VectorSyncState,
    vector_journal: IndexJournal,
    counters: DailyCounters,
) -> None:
    """Put the sweep under the process-wide scheduler.

    Infrastructure, not a business module: no `ModuleInfo`, no trigger route
    and no `RunJournal` entry — the sweep keeps its own `index_journal` in
    Redis. What it needs from the core is pacing, and that is all it takes.
    """
    if not vector_store.configured:
        logger.info("Vector store is not configured (qdrant_url), the sweep will not run")
        return

    async def interval() -> float:
        return (await config_store.get("vector", VectorConfig)).sweep_interval_seconds

    indexer = VectorIndexer(config_store, vector_sources, vector_store, vector_sync, vector_journal, counters)

    async def tick() -> SweepReport:
        # Which kind of tick this is, decided here and not by the scheduler:
        # a tick that arrived because the wait ran out is subject to the
        # interval, an "Index now" is precisely the request to ignore it.
        return await indexer.tick(paced=not tasks.was_woken(SWEEP_TASK))

    tasks.add(
        SWEEP_TASK,
        tick,
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
    `vector_sources(cfg)` when omitted) — tests inject fakes here instead of
    mocking iTop/repository internals.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        vector_sources: Callable[[VectorConfig], Sequence[VectorSource[Any]]],
        vector_store: ChunkStore,
        vector_sync: VectorSyncState,
        vector_journal: IndexJournal,
        counters: DailyCounters,
        sources: Sequence[VectorSource[Any]] | None = None,
    ) -> None:
        self._config_store = config_store
        self._vector_sources = vector_sources
        self._vector_store = vector_store
        self._vector_sync = vector_sync
        self._vector_journal = vector_journal
        self._counters = counters
        self._sources = list(sources) if sources is not None else None

    async def tick(self, *, paced: bool = False) -> SweepReport:
        report = await self.sweep_once(paced=paced)
        if report.status == "error":
            logger.warning(f"vector sweep finished with errors: {'; '.join(report.errors)}")
        return report

    async def request_reindex(self) -> None:
        """Schedule a full backfill: the next sweep resets all cursors and
        runs as kind="backfill". No truncate — unchanged chunks are cheap
        thanks to the hash-guard, and reconciliation cleans orphans."""
        await self._vector_sync.request_reindex()

    async def sweep_once(self, *, paced: bool = False) -> SweepReport:
        """`paced` subjects the pass to `sweep_interval_seconds` counted from
        the last one, wherever it ran — the loop under the scheduler passes it,
        every hand-driven caller (the CLI, "Index now") does not, because
        calling this by hand *is* the request for a pass."""
        if not self._vector_store.configured:
            return SweepReport(kind="sweep", status="skipped", skip_reason="qdrant_url is not set")
        vector_cfg = await self._config_store.get("vector", VectorConfig)
        if not vector_cfg.enabled:
            return SweepReport(kind="sweep", status="skipped", skip_reason="vector indexing is disabled")
        emb_cfg = await self._config_store.get("embeddings", EmbeddingsConfig)
        model = emb_cfg.model
        if not emb_cfg.base_url or not model:
            return SweepReport(kind="sweep", status="skipped", skip_reason="embeddings endpoint is not configured")
        if paced and (skip_reason := await self._too_soon(vector_cfg)) is not None:
            # Before the lock, so a paced-out tick leaves no `index_journal`
            # entry (opened in `_sweep_locked`): a restart must not be able to
            # push real passes out of the journal's capped window. Which is
            # also why it is logged here and not off the returned status in
            # `tick()`: the journal cannot say why the Vector page shows no
            # new run, and the other skips ("not configured", "disabled") hold
            # for every tick a deployment ever runs, so reporting those the
            # same way would be a log line a minute.
            logger.info(f"vector sweep skipped: {skip_reason}")
            return SweepReport(kind="sweep", status="skipped", skip_reason=skip_reason)

        async with self._vector_sync.sweep_lock() as locked:
            if not locked:
                return SweepReport(kind="sweep", status="skipped", skip_reason="another sweep holds the lock")
            report = await self._sweep_locked(self._vector_store, vector_cfg, emb_cfg, model)

        # Counted here and not in `tick()`, because the timer is not the only
        # caller: the backfill CLI (`use_cases/reindex.py`) drives this method
        # directly, and it is the largest embedding workload an installation
        # ever runs. The pass itself is not counted (REQ-009 R3 asks for
        # passes; a loop on a timer ticks the same number of times whatever
        # the installation does, so the number answers nothing). The work is:
        # how much of the customer's iTop the layer actually keeps embedded.
        await self._counters.bump(Counter.VECTOR_CHUNKS_EMBEDDED, report.chunks_embedded)
        return report

    async def _too_soon(self, cfg: VectorConfig) -> str | None:
        """Why a timer tick should not sweep yet, or None if it should.

        A requested backfill is never held back: the administrator asked for
        it, and the flag outlives the process it was asked in, so the tick
        that finds it standing is the one that has to honour it.
        """
        last = await self._vector_sync.get_swept()
        if last is None or await self._vector_sync.reindex_pending():
            return None
        elapsed = datetime.now(UTC) - last
        # A marker ahead of the clock sweeps now rather than waiting the
        # difference out: the host's clock can step backwards (an NTP
        # correction, a restored snapshot), and the marker has no TTL to
        # expire on its own — held against a negative elapsed, the gate would
        # close every tick until wall-clock time caught up with it.
        if elapsed < timedelta(0) or elapsed >= timedelta(seconds=cfg.sweep_interval_seconds):
            return None
        return (
            f"the last pass started {int(elapsed.total_seconds())}s ago, "
            f"inside the {cfg.sweep_interval_seconds}s sweep interval"
        )

    async def _sweep_locked(
        self, store: ChunkStore, cfg: VectorConfig, emb_cfg: EmbeddingsConfig, model: str
    ) -> SweepReport:
        started_at = datetime.now(UTC)
        # Stamped at the start and not on completion, so that a pass killed
        # halfway — a restart, a crash — still counts as one. Otherwise the
        # defect this gate exists for survives in a narrower form: a container
        # restarted mid-backfill would sweep again on every start. Nothing is
        # lost by the delay, because the per-class cursors survive a restart on
        # their own; the pass resumes at most one interval later.
        await self._vector_sync.set_swept(started_at)
        full = await self._vector_sync.reindex_pending()
        report = SweepReport(kind="backfill" if full else "sweep", status="ok")
        journal_id = await self._journal_start(report.kind)
        embedder = EmbeddingsClient(emb_cfg)
        try:
            if full:
                # Drops the pending-reindex flag along with the cursors — an
                # attempt that fails earlier leaves the request standing
                await self._vector_sync.reset_cursors()
            sources = self._sources if self._sources is not None else self._vector_sources(cfg)
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
                if not family_cfg.enabled:
                    # Debug, unlike the warning above: a family with no config
                    # entry is registry and config out of step, this one is
                    # what the administrator asked for. Before `prepare()`,
                    # `ensure_version` and `set_family_swept` alike — a
                    # switched-off family keeps the cursors it had, so
                    # switching it back on resumes instead of rebuilding.
                    logger.debug(f"vector sweep: family {family!r} is switched off — skipping")
                    continue
                if not source.classes:
                    continue
                # Only a family with its *own* interval gets paced against
                # its last real pass — the system-wide interval is enforced
                # before the pass ever starts (the scheduler's cadence, and
                # `_too_soon` across restarts), so gating an un-overridden
                # family here too would double up on the same value and could
                # skip an out-of-band tick ("Index now", or the scheduler
                # firing a hair early) that arrives before a full system
                # interval has elapsed since the last one.
                if not full and family_cfg.sweep_interval_seconds is not None:
                    last_swept = await self._vector_sync.get_family_swept(family)
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
                # `meta` is the version the store wants filled. When it is not
                # the active one, the embeddings model changed and this pass is
                # filling its replacement from empty — which every class below
                # reads off `meta` itself, no second flag and no state of its
                # own (`_sweep_class`'s `since`).
                errors_before = len(report.errors)
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
                if not meta.is_active:
                    await self._finish_rebuild(store, family, meta, report, clean=len(report.errors) == errors_before)
                await self._vector_sync.set_family_swept(family, started_at)
            if not report.errors:
                due = await self._families_to_reconcile(sources, cfg, datetime.now(UTC))
                if due:
                    await self._reconcile(store, sources, due, cfg, report, ensure_prepared)
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
                objects_skipped=report.objects_skipped,
                chunks_embedded=report.chunks_embedded,
                chunks_metadata_updated=report.chunks_metadata_updated,
                chunks_deleted=report.chunks_deleted,
                error="; ".join(report.errors) or None,
                warning=report.warning_text(),
            )
            await embedder.aclose()
        return report

    async def _finish_rebuild(
        self, store: ChunkStore, family: str, meta: IndexMeta, report: SweepReport, *, clean: bool
    ) -> None:
        """Switch the family over to the version this pass filled, or say why not.

        Only a pass that reported no error for this family may switch: an
        error means some class is short of objects the active version still
        has, and switching would delete them along with it. The family keeps
        answering from the old version and the next pass fills the same
        replacement further.

        "Without an error" is per family, not per pass — `tickets` failing
        must not hold back a replacement `kb_articles` has already finished,
        the same isolation the two catches above give the sweep itself.
        Objects the pass deliberately skipped (`objects_skipped`, TASK-073)
        are not errors and do not hold it back either: they are absent from
        the new version exactly as they would eventually be from the old one.
        Neither does a class the config leaves out — no entry under this
        family, or no chunk fragments chosen in it. Such a class is not walked
        at all, and the version this pass filled holds nothing for it exactly
        as a first indexing under the same config would hold nothing: the
        switch is what finally drops the chunks an earlier config left behind.
        """
        if not clean:
            logger.warning(
                f"vector sweep: family {family!r} v{meta.version} is not complete — the pass reported errors, "
                f"so the switch waits for a clean one; searches over {family!r} stay refused meanwhile"
            )
            return
        try:
            await store.activate_version(family, meta.version)
        except Exception as e:
            # Recorded rather than raised: the replacement is filled and
            # correct, only the switch failed, and the next pass will retry it.
            logger.exception(f"vector sweep: switching family {family!r} to v{meta.version} failed")
            report.errors.append(f"{family}: switching to v{meta.version} failed: {e}")

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
        plan = _chunk_plan(class_cfg)
        interval = family_cfg.sweep_interval_seconds or cfg.sweep_interval_seconds
        log_entries_per_chunk = family_cfg.log_entries_per_chunk or cfg.log_entries_per_chunk
        cursor = await self._vector_sync.get_cursor(obj_class)
        if meta.is_active:
            # Overlap covers pages drifting while a previous pass ran; derived
            # from the family's own interval instead of being one more config knob
            since = cursor - timedelta(seconds=2 * interval) if cursor else None
        else:
            # Filling a replacement version, which starts empty: the walk has
            # to be the whole class however far the increment had got. Reading
            # it off `meta` is what keeps the rebuild from needing a flag of
            # its own saying whether the cursors have already been reset for
            # it — a flag whose two writes could be interrupted halfway, and
            # the half that leaves it set makes the next pass go incremental
            # and switch a sparse version on as if it were complete.
            since = None
        max_seen = cursor
        page = 1
        while True:
            records = await source.find_modified_since(obj_class, since, page=page, page_size=cfg.sweep_page_size)
            # Embedding is batched per page: one embed() call for every changed
            # chunk of every object on it.
            pending: list[_Pending] = []
            for record in records:
                report.objects_seen += 1
                if record.updated_at and (max_seen is None or record.updated_at > max_seen):
                    max_seen = record.updated_at
                if left_indexable_scope(record.index_value, class_cfg.index_values):
                    # Left the indexable scope (e.g. reopened) — drop its chunks
                    report.chunks_deleted += await store.delete_object(family, obj_class, record.obj_id)
                    continue
                chunks = await source.chunk(
                    obj_class,
                    record,
                    plan,
                    max_chunk_tokens=cfg.max_chunk_tokens,
                    log_entries_per_chunk=log_entries_per_chunk,
                )
                size = sum(len(c.text) for c in chunks)
                logger.debug(f"vector sweep: {obj_class}::{record.obj_id} — {len(chunks)} chunks, {size} chars")
                if len(chunks) > cfg.max_chunks_per_object:
                    # Before the embed call, not after: the endpoint bills
                    # per text, and this object is junk. Whatever it has in
                    # the index already stays there — dropping a working
                    # article because its new revision is unusable would
                    # cost recall for nothing.
                    logger.error(
                        f"vector sweep: {obj_class}::{record.obj_id} produced {len(chunks)} chunks "
                        f"({size} chars), over max_chunks_per_object={cfg.max_chunks_per_object} "
                        f"— skipped without embedding"
                    )
                    report.objects_skipped += 1
                    report.warnings.append(
                        f"{obj_class}::{record.obj_id}: {len(chunks)} chunks over max_chunks_per_object"
                    )
                    continue
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
                    sync = classify_chunk(
                        chunk.content_hash,
                        chunk_meta.meta_hash,
                        stored_content_hash=digest.content_hash if digest else None,
                        stored_meta_hash=digest.meta_hash if digest else None,
                    )
                    if sync is ChunkSyncState.CHANGED:
                        changed.append((chunk, chunk_meta))
                    elif sync is ChunkSyncState.STALE_META:
                        stale_meta.append(chunk_meta)
                current_keys = {(c.kind, c.n) for c in chunks}
                vanished = [key for key in stored if key not in current_keys]
                if changed or stale_meta or vanished:
                    pending.append((record, changed, stale_meta, vanished))

            texts = [chunk.text for _, changed, _, _ in pending for chunk, _ in changed]
            if texts:
                logger.info(
                    f"vector sweep: {obj_class} page {page} — embedding {len(texts)} chunks "
                    f"({sum(len(t) for t in texts)} chars) of {sum(1 for _, changed, _, _ in pending if changed)} "
                    f"objects: {_embed_breakdown(obj_class, pending)}"
                )
            try:
                vectors = iter(await embedder.embed(texts) if texts else [])
            except Exception:
                # One call carries the whole page, so the traceback names no
                # object at all — without this the failure reads "class FAQ
                # failed" and nothing else.
                logger.error(
                    f"vector sweep: embedding {obj_class} page {page} failed, "
                    f"{len(texts)} chunks of {_embed_breakdown(obj_class, pending)}"
                )
                raise
            for record, changed, stale_meta, vanished in pending:
                # `vectors` is one iterator for the whole page: the records are
                # walked in the order their texts were embedded in.
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

        if max_seen is not None and max_seen != cursor and meta.is_active:
            await self._vector_sync.set_cursor(obj_class, max_seen)
        # A rebuild leaves the cursor exactly where it was, so it goes on
        # describing the version that is still answering searches. Advancing
        # it would strand every object modified while the replacement was
        # being filled: the increment would resume past them, and they only
        # ever reached the replacement — which an administrator putting the
        # previous model back throws away. The cost of not advancing it is one
        # over-wide increment after the switch, and the hash-guard makes that
        # a re-read rather than a re-embed. It is also what leaves a rebuild
        # entirely inside Qdrant: abandoning one costs nothing in Redis.

    async def _family_reconcile_due(self, family: str, cfg: VectorConfig, now: datetime) -> bool:
        last = await self._vector_sync.get_family_reconcile(family)
        return last is None or now - last >= timedelta(days=cfg.reconcile_interval_days)

    async def _families_to_reconcile(
        self, sources: Sequence[VectorSource[Any]], cfg: VectorConfig, now: datetime
    ) -> set[str]:
        """The families a reconcile pass would actually walk right now.

        Asked before the phase starts so a tick where every family is either
        switched off or not yet due skips it whole, journal entry included —
        the same answer the single global clock used to give, minus the case
        it got wrong.
        """
        due = set()
        for source in sources:
            family_cfg = cfg.families.get(source.name)
            if family_cfg is None or not family_cfg.enabled or not source.classes:
                continue
            if await self._family_reconcile_due(source.name, cfg, now):
                due.add(source.name)
        return due

    async def _reconcile(
        self,
        store: ChunkStore,
        sources: Sequence[VectorSource[Any]],
        due: set[str],
        cfg: VectorConfig,
        report: SweepReport,
        ensure_prepared: Callable[[VectorSource[Any]], Awaitable[None]],
    ) -> None:
        """Delete chunks of objects that no longer exist at their source
        (deleted or archived — invisible to the incremental sweep).

        Walks the families `_families_to_reconcile` picked — a family the
        sweep does not touch must not be reconciled either: nothing refreshes
        its chunks, but the source still answers `find_existing_ids`, so a due
        pass would delete the collection that family is supposed to keep. Each
        family's clock is stamped as it finishes, so one that fails does not
        cost the ones already walked their pass."""
        journal_id = await self._journal_start("reconcile")
        seen = deleted = 0
        status = "ok"
        error: str | None = None
        try:
            for source in sources:
                family = source.name
                if family not in due:
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
                await self._vector_sync.set_family_reconcile(family, datetime.now(UTC))
            await self._vector_sync.set_reconcile(datetime.now(UTC))
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
            return await self._vector_journal.start(kind)
        except Exception as e:
            logger.warning(f"index journal start failed (non-fatal): {e}")
            return None

    async def _journal_finish(self, journal_id: str | None, **kwargs) -> None:
        if journal_id is None:
            return
        try:
            await self._vector_journal.finish(journal_id, **kwargs)
        except Exception as e:
            logger.warning(f"index journal finish failed (non-fatal): {e}")


def _chunk_plan(class_cfg: VectorClassConfig) -> ChunkPlan:
    """`class_cfg.chunks` translated into the value `VectorSource.chunk()`
    actually takes (TASK-040) — computed once per class per pass, since
    `class_cfg` does not change within `_sweep_class`'s per-page loop."""
    return ChunkPlan(
        fields={kind: frag.fields for kind, frag in class_cfg.chunks.items()},
        enabled=frozenset(kind for kind, frag in class_cfg.chunks.items() if frag.enabled),
    )


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
        created_at=creation_date(
            record.created_at, record.updated_at, (d.created_at for d in stored.values()), started_at
        ),
        updated_at=record.updated_at,
    )
