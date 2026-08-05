import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis

from itop_ai_assistant.config import EmbeddingsConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.vector.chunker import chunk_object
from itop_ai_assistant.vector.index_journal import IndexJournal
from itop_ai_assistant.vector.indexer import SWEEP_TASK, VectorIndexer, register_vector_sweep
from itop_ai_assistant.vector.source import VectorRecord
from itop_ai_assistant.vector.store import ChunkDigest, ChunkMetadata, ChunkRecord, FingerprintMismatchError, IndexMeta
from itop_ai_assistant.vector.sync_state import VectorSyncState

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_FAMILY = "tickets"
_META = IndexMeta(family=_FAMILY, version=1, model="test-model", dim=4)
_LOCK_KEY = "vector:sweep:lock"

_VECTOR_CFG = VectorConfig(
    enabled=True,
    classes={"UserRequest": VectorClassConfig(index_values=["resolved", "closed"], profile={"body": ["description"]})},
    sweep_interval_seconds=300,
    sweep_throttle_seconds=0,
)
_EMB_CFG = EmbeddingsConfig(base_url="http://emb/v1", model="test-model", dimension=4)


def _flat(calls: list[list]) -> list:
    """The indexer calls a write op once per pending object, empty list
    included when that object had nothing for that particular bucket — flatten
    to inspect what was actually written."""
    return [item for call in calls for item in call]


def _record(
    obj_id: int, *, index_value: str = "resolved", description: str = "Broken.", last_update: datetime = _NOW
) -> VectorRecord:
    return VectorRecord(
        obj_id=obj_id,
        index_value=index_value,
        last_update=last_update,
        created_at=_NOW - timedelta(days=1),
        payload={"description": description},
    )


class FakeTicketSource:
    """Stand-in for TicketVectorSource: same VectorSource contract, no iTop."""

    def __init__(
        self,
        records: list[VectorRecord] | None = None,
        *,
        classes: tuple[str, ...] = ("UserRequest",),
        name: str = _FAMILY,
    ):
        self.name = name
        self.classes = list(classes)
        self._records = records or []
        self.prepare_calls = 0
        self.find_modified_since_calls: list[tuple] = []
        self.find_modified_since_error: Exception | None = None
        self.find_existing_ids_result: set[int] | None = None

    async def prepare(self) -> None:
        self.prepare_calls += 1

    async def find_modified_since(self, obj_class, since, *, page, page_size) -> list[VectorRecord]:
        self.find_modified_since_calls.append((obj_class, since, page, page_size))
        if self.find_modified_since_error:
            raise self.find_modified_since_error
        return self._records if page == 1 else []

    async def find_existing_ids(self, obj_class, ids) -> set[int]:
        if self.find_existing_ids_result is not None:
            return self.find_existing_ids_result
        return set(ids)

    async def chunk(self, obj_class, record, profile, *, max_chunk_tokens, log_entries_per_chunk):
        return chunk_object(
            record.payload, profile, max_chunk_tokens=max_chunk_tokens, log_entries_per_chunk=log_entries_per_chunk
        )


class FakeChunkStore:
    """In-memory ChunkStore: the indexer's contract, none of the storage.

    State keyed by `(family, obj_class, obj_id)` throughout — a family is
    just another dimension of the key, the same way `obj_class` already was
    (TASK-008)."""

    def __init__(self, meta: IndexMeta = _META, *, configured: bool = True):
        self.configured = configured
        self._meta = meta
        # Mimics a real backend: writes land here too, so a second sweep
        # pass over an unchanged object sees what the first one wrote.
        self.digests: dict[tuple[str, str, int], dict[tuple[str, int], ChunkDigest]] = {}
        self.upsert_calls: list[list[ChunkRecord]] = []
        self.update_metadata_calls: list[list[ChunkMetadata]] = []
        self.delete_chunks_calls: list[tuple[str, str, int, list[tuple[str, int]]]] = []
        self.delete_object_calls: list[tuple[str, str, int]] = []
        self.delete_object_return = 3  # matches the previous mock's fixed return value
        self.list_object_ids_calls = 0
        self.list_object_ids_side_effect = None
        self.ensure_version_error: Exception | None = None
        # Per-family override — lets a test fail one family's fingerprint
        # check without touching another's in the same pass.
        self.ensure_version_errors: dict[str, Exception] = {}

    async def ensure_version(self, family, model, dim) -> IndexMeta:
        if family in self.ensure_version_errors:
            raise self.ensure_version_errors[family]
        if self.ensure_version_error:
            raise self.ensure_version_error
        return IndexMeta(family=family, version=self._meta.version, model=self._meta.model, dim=self._meta.dim)

    async def active_meta(self, family) -> IndexMeta | None:
        return IndexMeta(family=family, version=self._meta.version, model=self._meta.model, dim=self._meta.dim)

    async def upsert_chunks(self, chunks, *, family, model, dim) -> int:
        self.upsert_calls.append(list(chunks))
        for c in chunks:
            key = (family, c.meta.obj_class, c.meta.obj_id)
            self.digests.setdefault(key, {})[(c.meta.chunk_kind, c.meta.chunk_n)] = ChunkDigest(
                content_hash=c.meta.content_hash, meta_hash=c.meta.meta_hash
            )
        return len(chunks)

    async def get_chunk_digests(self, family, obj_class, obj_id) -> dict[tuple[str, int], ChunkDigest]:
        return self.digests.get((family, obj_class, obj_id), {})

    async def update_chunk_metadata(self, chunks, *, family) -> int:
        self.update_metadata_calls.append(list(chunks))
        for c in chunks:
            key = (family, c.obj_class, c.obj_id)
            self.digests.setdefault(key, {})[(c.chunk_kind, c.chunk_n)] = ChunkDigest(
                content_hash=c.content_hash, meta_hash=c.meta_hash
            )
        return len(chunks)

    async def delete_chunks(self, family, obj_class, obj_id, keys) -> int:
        self.delete_chunks_calls.append((family, obj_class, obj_id, list(keys)))
        return len(keys)

    async def delete_object(self, family, obj_class, obj_id) -> int:
        self.delete_object_calls.append((family, obj_class, obj_id))
        return self.delete_object_return

    async def list_object_ids(self, family, obj_class, after=0, limit=1000) -> list[int]:
        self.list_object_ids_calls += 1
        if self.list_object_ids_side_effect:
            return self.list_object_ids_side_effect(family, obj_class, after, limit)
        return []

    async def search(self, embedding, **kwargs):
        return []

    async def stats(self, family):
        return None

    async def aclose(self) -> None:
        return None


class FlakyStartJournal(IndexJournal):
    """Journal whose start() always fails — start failures must be non-fatal."""

    def __init__(self, redis):
        super().__init__(redis)
        self.finish_calls = 0

    async def start(self, kind: str) -> str:
        raise RuntimeError("pg hiccup")

    async def finish(self, *args, **kwargs) -> None:
        self.finish_calls += 1
        await super().finish(*args, **kwargs)


def _embedder_mock() -> MagicMock:
    embedder = MagicMock()
    embedder.embed = AsyncMock(side_effect=lambda texts: [[0.0] * 4 for _ in texts])
    embedder.aclose = AsyncMock()
    return embedder


def _deps_mock(
    *,
    vector_cfg=_VECTOR_CFG,
    emb_cfg=_EMB_CFG,
    configured=True,
    store: FakeChunkStore | None = None,
    journal: IndexJournal | None = None,
) -> MagicMock:
    deps = MagicMock()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    deps.vector_store = store if store is not None else FakeChunkStore(configured=configured)
    deps.vector_sync = VectorSyncState(redis)
    deps.vector_journal = journal if journal is not None else IndexJournal(redis)
    deps.config_store.get = AsyncMock(
        side_effect=lambda name, model: {"vector": vector_cfg, "embeddings": emb_cfg}[name]
    )
    return deps


class IndexerTestCase(unittest.IsolatedAsyncioTestCase):
    async def _run(self, deps, source, embedder=None):
        return await self._run_sources(deps, [source], embedder=embedder)

    async def _run_sources(self, deps, sources, embedder=None):
        indexer = VectorIndexer(deps, sources=sources)
        self.indexer = indexer
        embedder = embedder or _embedder_mock()
        self.embedder = embedder
        with patch("itop_ai_assistant.vector.indexer.EmbeddingsClient", return_value=embedder):
            return await indexer.sweep_once()


class TestSkips(IndexerTestCase):
    async def test_skip_when_db_not_configured(self):
        report = await self._run(_deps_mock(configured=False), FakeTicketSource())

        self.assertEqual(report.status, "skipped")
        self.assertIn("qdrant_url", report.skip_reason)

    async def test_skip_when_disabled(self):
        deps = _deps_mock(vector_cfg=VectorConfig(enabled=False))
        report = await self._run(deps, FakeTicketSource())

        self.assertEqual(report.status, "skipped")
        self.assertIn("disabled", report.skip_reason)

    async def test_skip_when_embeddings_missing(self):
        deps = _deps_mock(emb_cfg=EmbeddingsConfig(base_url=None, model=None))
        report = await self._run(deps, FakeTicketSource())

        self.assertEqual(report.status, "skipped")
        self.assertIn("embeddings", report.skip_reason)

    async def test_skip_when_lock_not_acquired(self):
        deps = _deps_mock()
        await deps.vector_sync._redis.set(_LOCK_KEY, "someone-else", ex=120)
        report = await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "skipped")
        self.assertIn("lock", report.skip_reason)
        self.assertEqual(await deps.vector_journal.recent(), [])

    async def test_no_source_registered_for_class_is_reported_as_ok(self):
        # A class in cfg.classes that no registered source claims — logged
        # and skipped, same tolerance as "no chunking profile".
        source = FakeTicketSource([_record(1)], classes=("SomeOtherClass",))
        report = await self._run(_deps_mock(), source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.objects_seen, 0)
        self.assertEqual(source.prepare_calls, 0)


class TestMultiFamily(IndexerTestCase):
    """D1/D4, TASK-008: each `VectorSource.name` is its own collection, and a
    fingerprint mismatch (or any other failure) in one family isolates to
    that family's classes, the same way a single class's failure already
    isolated to that class."""

    async def test_two_sources_claiming_the_same_class_is_an_error(self):
        a = FakeTicketSource([_record(1)], classes=("UserRequest",), name="tickets")
        b = FakeTicketSource([_record(2)], classes=("UserRequest",), name="tickets2")
        report = await self._run_sources(_deps_mock(), [a, b])

        self.assertEqual(report.status, "error")
        self.assertIn("UserRequest", report.errors[0])
        self.assertIn("tickets", report.errors[0])
        self.assertIn("tickets2", report.errors[0])
        self.assertEqual(a.prepare_calls, 0)

    async def test_fingerprint_mismatch_of_one_family_does_not_block_another(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.classes["KnowledgeBaseArticle"] = VectorClassConfig(index_values=[], profile={"body": ["description"]})
        store = FakeChunkStore()
        store.ensure_version_errors = {"kb_articles": FingerprintMismatchError("dim changed")}
        tickets = FakeTicketSource([_record(1)], classes=("UserRequest",), name="tickets")
        kb = FakeTicketSource([_record(2)], classes=("KnowledgeBaseArticle",), name="kb_articles")
        deps = _deps_mock(vector_cfg=cfg, store=store)

        report = await self._run_sources(deps, [tickets, kb])

        self.assertEqual(report.status, "error")
        self.assertIn("rebuild required", "; ".join(report.errors))
        self.assertEqual(tickets.prepare_calls, 1)
        self.assertEqual(kb.prepare_calls, 1)
        # tickets synced despite kb_articles' fingerprint mismatch
        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(store.upsert_calls[-1][0].meta.obj_class, "UserRequest")


class TestSweep(IndexerTestCase):
    async def test_embeds_and_upserts_new_ticket(self):
        store = FakeChunkStore()
        source = FakeTicketSource([_record(1)])
        report = await self._run(_deps_mock(store=store), source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.kind, "sweep")
        self.assertEqual(report.objects_seen, 1)
        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(source.prepare_calls, 1)
        records = store.upsert_calls[-1]
        self.assertEqual(records[0].meta.obj_id, 1)
        self.assertEqual(records[0].meta.chunk_kind, "body")
        self.assertEqual(records[0].meta.status, "resolved")

    async def test_hash_guard_skips_unchanged(self):
        store = FakeChunkStore()
        source = FakeTicketSource([_record(1)])
        await self._run(_deps_mock(store=store), source)

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        embedder2 = _embedder_mock()
        report = await self._run(_deps_mock(store=store2), FakeTicketSource([_record(1)]), embedder2)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_metadata_updated, 0)
        embedder2.embed.assert_not_awaited()
        self.assertEqual(store2.upsert_calls, [])
        self.assertEqual(store2.update_metadata_calls, [])

    async def test_vanished_chunks_deleted(self):
        store = FakeChunkStore()
        store.digests[(_FAMILY, "UserRequest", 1)] = {
            ("body", 0): ChunkDigest(content_hash="stale", meta_hash=None),
            ("body", 5): ChunkDigest(content_hash="gone", meta_hash=None),
        }
        report = await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        self.assertEqual(store.delete_chunks_calls, [(_FAMILY, "UserRequest", 1, [("body", 5)])])
        self.assertEqual(report.chunks_deleted, 1)
        self.assertEqual(report.chunks_embedded, 1)  # ("body", 0) hash mismatch → re-embedded

    async def test_object_out_of_index_values_deleted(self):
        store = FakeChunkStore()
        report = await self._run(_deps_mock(store=store), FakeTicketSource([_record(1, index_value="new")]))

        self.assertEqual(store.delete_object_calls, [(_FAMILY, "UserRequest", 1)])
        self.assertEqual(report.chunks_deleted, 3)
        self.assertEqual(store.upsert_calls, [])

    async def test_empty_index_values_indexes_everything(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.classes["UserRequest"].index_values = []
        store = FakeChunkStore()
        report = await self._run(
            _deps_mock(vector_cfg=cfg, store=store), FakeTicketSource([_record(1, index_value="new")])
        )

        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(store.delete_object_calls, [])

    async def test_cursor_set_to_max_last_update(self):
        newest = _NOW + timedelta(hours=2)
        deps = _deps_mock()
        source = FakeTicketSource([_record(1, last_update=_NOW), _record(2, last_update=newest)])
        await self._run(deps, source)

        self.assertEqual(await deps.vector_sync.get_cursor("UserRequest"), newest)

    async def test_since_is_cursor_minus_double_interval(self):
        cursor = _NOW
        deps = _deps_mock()
        await deps.vector_sync.set_cursor("UserRequest", cursor)
        await deps.vector_sync.set_reconcile(datetime.now(UTC))  # reconcile not due
        source = FakeTicketSource([])
        await self._run(deps, source)

        since = source.find_modified_since_calls[-1][1]
        self.assertEqual(since, cursor - timedelta(seconds=2 * _VECTOR_CFG.sweep_interval_seconds))

    async def test_class_error_keeps_cursor_and_reports(self):
        deps = _deps_mock()
        source = FakeTicketSource([])
        source.find_modified_since_error = RuntimeError("itop down")
        report = await self._run(deps, source)

        self.assertEqual(report.status, "error")
        self.assertIn("itop down", report.errors[0])
        self.assertEqual(await deps.vector_sync.list_cursors(), {})
        entries = await deps.vector_journal.recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "error")

    async def test_fingerprint_mismatch_is_journaled_error(self):
        store = FakeChunkStore()
        store.ensure_version_error = FingerprintMismatchError("dim changed")
        deps = _deps_mock(store=store)
        report = await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "error")
        self.assertIn("rebuild required", report.errors[0])
        entries = await deps.vector_journal.recent()
        self.assertEqual(entries[0]["status"], "error")

    async def test_journal_failure_is_non_fatal(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        journal = FlakyStartJournal(redis)
        deps = _deps_mock(journal=journal)
        report = await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(journal.finish_calls, 0)


class TestMetadataFreshness(IndexerTestCase):
    """R6/ADR-004: status (and the rest of the filterable payload) reaches
    the index without a re-embed when only it — not the chunk text —
    changed. See TASK-003."""

    async def test_status_change_only_updates_metadata(self):
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1, index_value="resolved")]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        embedder2 = _embedder_mock()
        report = await self._run(
            _deps_mock(store=store2), FakeTicketSource([_record(1, index_value="closed")]), embedder2
        )

        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_metadata_updated, 1)
        embedder2.embed.assert_not_awaited()
        self.assertEqual(_flat(store2.upsert_calls), [])
        updated = _flat(store2.update_metadata_calls)
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].status, "closed")

    async def test_last_update_reaches_the_chunk(self):
        # The date the "solved within the last year" filter runs on — it comes
        # from the source and used to stop at the indexer (TASK-004)
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        self.assertEqual(_flat(store.upsert_calls)[0].meta.last_update, _NOW)

    async def test_a_moved_last_update_alone_updates_metadata(self):
        # The reopen-and-close-again case: same text, newer date. Without the
        # rewrite the window filter would answer from the first indexing forever
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        later = _NOW + timedelta(days=30)
        embedder2 = _embedder_mock()
        report = await self._run(_deps_mock(store=store2), FakeTicketSource([_record(1, last_update=later)]), embedder2)

        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_metadata_updated, 1)
        embedder2.embed.assert_not_awaited()
        self.assertEqual(_flat(store2.update_metadata_calls)[0].last_update, later)

    async def test_text_change_only_embeds_metadata_not_rewritten_twice(self):
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1, description="Broken.")]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        report = await self._run(_deps_mock(store=store2), FakeTicketSource([_record(1, description="Still broken.")]))

        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(report.chunks_metadata_updated, 0)
        self.assertEqual(_flat(store2.update_metadata_calls), [])

    async def test_legacy_chunk_without_meta_hash_gets_one_metadata_rewrite(self):
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))
        digest = store.digests[(_FAMILY, "UserRequest", 1)][("body", 0)]

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = {
            ("body", 0): ChunkDigest(content_hash=digest.content_hash, meta_hash=None)
        }
        report = await self._run(_deps_mock(store=store2), FakeTicketSource([_record(1)]))

        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_metadata_updated, 1)
        self.assertEqual(_flat(store2.upsert_calls), [])

    async def test_mixed_object_takes_both_paths_in_one_pass(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.classes["UserRequest"].profile = {"body": ["description"], "solution": ["resolution"]}
        original = VectorRecord(
            obj_id=1,
            index_value="resolved",
            last_update=_NOW,
            created_at=_NOW - timedelta(days=1),
            payload={"description": "Broken.", "resolution": "Rebooted it."},
        )
        store = FakeChunkStore()
        await self._run(_deps_mock(vector_cfg=cfg, store=store), FakeTicketSource([original]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        reopened = VectorRecord(
            obj_id=1,
            index_value="closed",
            last_update=_NOW,
            created_at=_NOW - timedelta(days=1),
            payload={"description": "Still broken.", "resolution": "Rebooted it."},
        )
        report = await self._run(_deps_mock(vector_cfg=cfg, store=store2), FakeTicketSource([reopened]))

        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(report.chunks_metadata_updated, 1)
        self.assertEqual(store2.upsert_calls[-1][0].meta.chunk_kind, "body")
        self.assertEqual(store2.update_metadata_calls[-1][0].chunk_kind, "solution")

    async def test_idempotent_second_pass_writes_nothing(self):
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        report = await self._run(_deps_mock(store=store2), FakeTicketSource([_record(1)]))

        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_metadata_updated, 0)
        self.assertEqual(report.chunks_deleted, 0)
        self.assertEqual(store2.upsert_calls, [])
        self.assertEqual(store2.update_metadata_calls, [])
        self.assertEqual(store2.delete_chunks_calls, [])


class TestSweepRegistration(unittest.IsolatedAsyncioTestCase):
    """Infrastructure, not a module: the sweep takes pacing from the scheduler
    and claims no trigger route."""

    async def test_registers_a_loop_when_the_database_is_configured(self):
        tasks = PeriodicTasks()
        register_vector_sweep(tasks, _deps_mock())

        self.assertEqual(tasks.names, [SWEEP_TASK])

    async def test_no_loop_without_a_database(self):
        tasks = PeriodicTasks()
        register_vector_sweep(tasks, _deps_mock(configured=False))

        self.assertEqual(tasks.names, [])

    async def test_interval_comes_from_the_runtime_config(self):
        tasks = PeriodicTasks()
        deps = _deps_mock(vector_cfg=_VECTOR_CFG.model_copy(update={"sweep_interval_seconds": 42}))
        register_vector_sweep(tasks, deps)

        self.assertEqual(await tasks._entries[SWEEP_TASK].interval(), 42)


class TestReindex(IndexerTestCase):
    """The pending backfill lives in Redis, not in the sweeping process: the
    request survives a restart and is honoured by whichever replica wins the
    sweep lock."""

    async def test_request_reindex_marks_it_in_the_index(self):
        deps = _deps_mock()
        indexer = VectorIndexer(deps)
        await indexer.request_reindex()

        self.assertTrue(await deps.vector_sync.reindex_pending())

    async def test_pending_request_resets_cursors_and_runs_backfill(self):
        deps = _deps_mock()
        await deps.vector_sync.set_cursor("UserRequest", _NOW)
        await deps.vector_sync.request_reindex()
        # reset_cursors drops the pending flag too — that is what clears it.
        # Spy rather than asserting a post-run empty state: a backfill that
        # successfully re-indexes the object sets the cursor right back.
        original_reset = deps.vector_sync.reset_cursors
        reset_calls = []

        async def spy_reset() -> None:
            reset_calls.append(1)
            await original_reset()

        deps.vector_sync.reset_cursors = spy_reset

        report = await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertEqual(report.kind, "backfill")
        self.assertEqual(len(reset_calls), 1)
        self.assertFalse(await deps.vector_sync.reindex_pending())
        kinds = [e["kind"] for e in await deps.vector_journal.recent()]
        self.assertIn("backfill", kinds)

    async def test_a_totally_broken_store_still_resets_cursors(self):
        # ensure_version moved inside the per-class loop (D4, TASK-008): the
        # cursor reset that defines a backfill now runs before any family's
        # fingerprint is checked, since it is no longer gated by one upfront
        # global call. A class whose family fails still loses its cursor —
        # that costs the "backfill" label on the retry, not correctness: a
        # dropped cursor already reads as "sweep everything" on its own.
        store = FakeChunkStore()
        store.ensure_version_error = RuntimeError("pg down")
        deps = _deps_mock(store=store)
        await deps.vector_sync.set_cursor("UserRequest", _NOW)
        await deps.vector_sync.request_reindex()
        report = await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "error")
        self.assertIsNone(await deps.vector_sync.get_cursor("UserRequest"))
        self.assertFalse(await deps.vector_sync.reindex_pending())


class TestReconciliation(IndexerTestCase):
    async def test_due_when_never_ran_and_deletes_orphans(self):
        store = FakeChunkStore()
        store.list_object_ids_side_effect = lambda family, cls, after, limit: [1, 2] if after == 0 else []
        source = FakeTicketSource([])
        source.find_existing_ids_result = {1}
        deps = _deps_mock(store=store)
        report = await self._run(deps, source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(store.delete_object_calls, [(_FAMILY, "UserRequest", 2)])
        kinds = [e["kind"] for e in await deps.vector_journal.recent()]
        self.assertIn("reconcile", kinds)
        self.assertIsNotNone(await deps.vector_sync.get_reconcile())

    async def test_not_due_when_recent(self):
        store = FakeChunkStore()
        deps = _deps_mock(store=store)
        await deps.vector_sync.set_reconcile(datetime.now(UTC) - timedelta(days=1))
        report = await self._run(deps, FakeTicketSource([]))

        self.assertEqual(report.status, "ok")
        self.assertEqual(store.list_object_ids_calls, 0)
        kinds = [e["kind"] for e in await deps.vector_journal.recent()]
        self.assertNotIn("reconcile", kinds)

    async def test_skipped_after_class_errors(self):
        store = FakeChunkStore()
        source = FakeTicketSource([])
        source.find_modified_since_error = RuntimeError("boom")
        deps = _deps_mock(store=store)
        await self._run(deps, source)

        self.assertEqual(store.list_object_ids_calls, 0)


if __name__ == "__main__":
    unittest.main()
