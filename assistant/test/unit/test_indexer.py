import inspect
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis

from itop_ai_assistant.config import EmbeddingsConfig
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.vector.chunker import FragmentContent, TextContent, chunk_object
from itop_ai_assistant.vector.config import ChunkFragmentConfig, FamilyConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.vector.ports.source import VectorRecord
from itop_ai_assistant.vector.ports.store import (
    ChunkDigest,
    ChunkMetadata,
    ChunkRecord,
    FingerprintMismatchError,
    IndexMeta,
)
from itop_ai_assistant.vector.state.index_journal import IndexJournal
from itop_ai_assistant.vector.state.sync_state import VectorSyncState
from itop_ai_assistant.vector.use_cases.indexer import (
    SWEEP_TASK,
    VectorIndexer,
    register_vector_sweep,
)

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_FAMILY = "tickets"
_META = IndexMeta(family=_FAMILY, version=1, model="test-model", dim=4)
_LOCK_KEY = "vector:sweep:lock"

_VECTOR_CFG = VectorConfig(
    enabled=True,
    families={
        "tickets": FamilyConfig(
            classes={
                "UserRequest": VectorClassConfig(
                    index_values=["resolved", "closed"],
                    chunks={"body": ChunkFragmentConfig(fields=["description"])},
                )
            }
        )
    },
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
    obj_id: int,
    *,
    index_value: str = "resolved",
    description: str = "Broken.",
    updated_at: datetime = _NOW,
    created_at: datetime | None = _NOW - timedelta(days=1),
    payload: dict | None = None,
) -> VectorRecord:
    return VectorRecord(
        obj_id=obj_id,
        index_value=index_value,
        updated_at=updated_at,
        created_at=created_at,
        payload=payload if payload is not None else {"body": description},
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
        self.indexed_filter_keys: tuple[str, ...] = ()
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

    async def chunk(self, obj_class, record, plan, *, max_chunk_tokens, log_entries_per_chunk):
        # Resolving a plan into fragment content is the real source's job;
        # this probe maps each configured fragment to the payload field of
        # the same name and stops there.
        fragments = [
            FragmentContent(kind=kind, visibility="public", content=TextContent(record.payload[kind]))
            for kind in set(plan.fields) | plan.enabled
            if kind in record.payload
        ]
        return chunk_object(fragments, max_chunk_tokens=max_chunk_tokens, items_per_window=log_entries_per_chunk)


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
        # Objects whose write blows up — one oversized object among healthy ones.
        self.upsert_error_ids: set[int] = set()
        # Per-family override — lets a test fail one family's fingerprint
        # check without touching another's in the same pass.
        self.ensure_version_errors: dict[str, Exception] = {}

    async def ensure_version(self, family, model, dim, *, filter_keys=()) -> IndexMeta:
        if family in self.ensure_version_errors:
            raise self.ensure_version_errors[family]
        if self.ensure_version_error:
            raise self.ensure_version_error
        return IndexMeta(family=family, version=self._meta.version, model=self._meta.model, dim=self._meta.dim)

    async def active_meta(self, family) -> IndexMeta | None:
        return IndexMeta(family=family, version=self._meta.version, model=self._meta.model, dim=self._meta.dim)

    async def upsert_chunks(self, chunks, *, family, model, dim) -> int:
        if any(c.meta.obj_id in self.upsert_error_ids for c in chunks):
            raise RuntimeError("request too large")
        self.upsert_calls.append(list(chunks))
        for c in chunks:
            key = (family, c.meta.obj_class, c.meta.obj_id)
            self.digests.setdefault(key, {})[(c.meta.chunk_kind, c.meta.chunk_n)] = ChunkDigest(
                content_hash=c.meta.content_hash, meta_hash=c.meta.meta_hash, created_at=c.meta.created_at
            )
        return len(chunks)

    async def get_chunk_digests(self, family, obj_class, obj_id) -> dict[tuple[str, int], ChunkDigest]:
        return self.digests.get((family, obj_class, obj_id), {})

    async def update_chunk_metadata(self, chunks, *, family) -> int:
        self.update_metadata_calls.append(list(chunks))
        for c in chunks:
            key = (family, c.obj_class, c.obj_id)
            self.digests.setdefault(key, {})[(c.chunk_kind, c.chunk_n)] = ChunkDigest(
                content_hash=c.content_hash, meta_hash=c.meta_hash, created_at=c.created_at
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
    deps.counters = DailyCounters(redis)
    return deps


def _indexer(deps, sources=None) -> VectorIndexer:
    return VectorIndexer(
        deps.config_store,
        deps.vector_sources,
        deps.vector_store,
        deps.vector_sync,
        deps.vector_journal,
        deps.counters,
        sources=sources,
    )


def _register_sweep(tasks, deps) -> None:
    register_vector_sweep(
        tasks,
        deps.config_store,
        deps.vector_sources,
        deps.vector_store,
        deps.vector_sync,
        deps.vector_journal,
        deps.counters,
    )


class IndexerTestCase(unittest.IsolatedAsyncioTestCase):
    async def _run(self, deps, source, embedder=None):
        return await self._run_sources(deps, [source], embedder=embedder)

    async def _run_sources(self, deps, sources, embedder=None):
        indexer = _indexer(deps, sources)
        self.indexer = indexer
        embedder = embedder or _embedder_mock()
        self.embedder = embedder
        with patch("itop_ai_assistant.vector.use_cases.indexer.EmbeddingsClient", return_value=embedder):
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

    async def test_no_config_entry_for_family_is_reported_as_ok(self):
        # A source whose name matches nothing in cfg.families — logged and
        # skipped before prepare() is ever called, same tolerance as an
        # unrecognized family key in the saved config (TASK-021).
        source = FakeTicketSource([_record(1)], classes=("UserRequest",), name="unknown-family")
        report = await self._run(_deps_mock(), source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.objects_seen, 0)
        self.assertEqual(source.prepare_calls, 0)

    async def test_class_not_in_family_config_is_skipped_after_prepare(self):
        # Unlike a whole unrecognized family, a class the family's own config
        # does not list is only discovered after the family itself has
        # already been prepared — this pairing cannot arise from a real
        # config (registry.py builds a source's classes from the same
        # FamilyConfig), only from a source injected directly, as in tests.
        source = FakeTicketSource([_record(1)], classes=("SomeOtherClass",), name="tickets")
        report = await self._run(_deps_mock(), source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.objects_seen, 0)
        self.assertEqual(source.prepare_calls, 1)


class TestMultiFamily(IndexerTestCase):
    """D1/D4, TASK-008: each `VectorSource.name` is its own collection, and a
    fingerprint mismatch (or any other failure) in one family isolates to
    that family's classes, the same way a single class's failure already
    isolated to that class."""

    async def test_same_class_under_two_families_is_independent(self):
        # TASK-021 M:N: a class can be a key under several families at once,
        # each with its own independent config — no longer a collision to
        # guard against, since sweep goes by source, not by a merged
        # class→source map.
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets2"] = FamilyConfig(
            classes={"UserRequest": VectorClassConfig(chunks={"body": ChunkFragmentConfig(fields=["description"])})}
        )
        a = FakeTicketSource([_record(1)], classes=("UserRequest",), name="tickets")
        b = FakeTicketSource([_record(2)], classes=("UserRequest",), name="tickets2")
        store = FakeChunkStore()
        deps = _deps_mock(vector_cfg=cfg, store=store)

        report = await self._run_sources(deps, [a, b])

        self.assertEqual(report.status, "ok")
        self.assertEqual(a.prepare_calls, 1)
        self.assertEqual(b.prepare_calls, 1)
        self.assertEqual(report.chunks_embedded, 2)
        self.assertIn((_FAMILY, "UserRequest", 1), store.digests)
        self.assertIn(("tickets2", "UserRequest", 2), store.digests)

    async def test_fingerprint_mismatch_of_one_family_does_not_block_another(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["kb_articles"] = FamilyConfig(
            classes={
                "KnowledgeBaseArticle": VectorClassConfig(
                    index_values=[], chunks={"body": ChunkFragmentConfig(fields=["description"])}
                )
            }
        )
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

    async def test_generic_failure_preparing_one_family_does_not_block_another(self):
        # Same isolation, for a plain infrastructure error rather than a
        # fingerprint mismatch — the class docstring's "or any other failure".
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["kb_articles"] = FamilyConfig(
            classes={
                "KnowledgeBaseArticle": VectorClassConfig(chunks={"body": ChunkFragmentConfig(fields=["description"])})
            }
        )
        store = FakeChunkStore()
        store.ensure_version_errors = {"kb_articles": RuntimeError("qdrant down")}
        tickets = FakeTicketSource([_record(1)], classes=("UserRequest",), name="tickets")
        kb = FakeTicketSource([_record(2)], classes=("KnowledgeBaseArticle",), name="kb_articles")
        deps = _deps_mock(vector_cfg=cfg, store=store)

        report = await self._run_sources(deps, [tickets, kb])

        self.assertEqual(report.status, "error")
        self.assertIn("qdrant down", "; ".join(report.errors))
        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(store.upsert_calls[-1][0].meta.obj_class, "UserRequest")

    async def test_fingerprint_mismatch_reported_once_not_per_class(self):
        # ensure_version is now called once per source, not once per class of
        # a multi-class family — a mismatch must not produce a duplicate
        # error for the family's second class.
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets"].classes["Incident"] = VectorClassConfig(
            chunks={"body": ChunkFragmentConfig(fields=["description"])}
        )
        store = FakeChunkStore()
        store.ensure_version_error = FingerprintMismatchError("dim changed")
        deps = _deps_mock(vector_cfg=cfg, store=store)
        await deps.vector_sync.set_reconcile(datetime.now(UTC))
        source = FakeTicketSource([_record(1)], classes=("UserRequest", "Incident"))

        report = await self._run(deps, source)

        self.assertEqual(report.status, "error")
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(source.prepare_calls, 1)


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
        self.assertEqual(records[0].meta.filters["status"], "resolved")

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
        cfg.families["tickets"].classes["UserRequest"].index_values = []
        store = FakeChunkStore()
        report = await self._run(
            _deps_mock(vector_cfg=cfg, store=store), FakeTicketSource([_record(1, index_value="new")])
        )

        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(store.delete_object_calls, [])

    async def test_cursor_set_to_max_last_update(self):
        newest = _NOW + timedelta(hours=2)
        deps = _deps_mock()
        source = FakeTicketSource([_record(1, updated_at=_NOW), _record(2, updated_at=newest)])
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

    async def test_an_object_over_the_chunk_limit_is_never_embedded(self):
        # The endpoint bills per text, so the guard has to fire before embed(),
        # not after a failed write.
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.max_chunks_per_object = 2
        cfg.max_chunk_tokens = 1  # 3 chars a chunk — a short body is already over
        store = FakeChunkStore()
        embedder = _embedder_mock()
        deps = _deps_mock(vector_cfg=cfg, store=store)
        report = await self._run(deps, FakeTicketSource([_record(1, description="a much longer body")]), embedder)

        self.assertEqual(report.status, "error")
        self.assertIn("max_chunks_per_object", report.errors[0])
        self.assertIn("UserRequest:1", report.errors[0])
        embedder.embed.assert_not_awaited()
        self.assertEqual(store.upsert_calls, [])

    async def test_an_object_within_the_chunk_limit_is_indexed(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.max_chunks_per_object = 2
        store = FakeChunkStore()
        report = await self._run(_deps_mock(vector_cfg=cfg, store=store), FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 1)

    async def test_a_failing_object_does_not_stop_the_class(self):
        # Without per-object isolation the pass returns before the cursor is
        # written, so the same page is re-read on every tick forever.
        store = FakeChunkStore()
        store.upsert_error_ids = {1}
        newest = _NOW + timedelta(hours=2)
        deps = _deps_mock(store=store)
        report = await self._run(deps, FakeTicketSource([_record(1), _record(2, updated_at=newest)]))

        self.assertEqual(report.status, "error")
        self.assertIn("UserRequest:1", report.errors[0])
        self.assertEqual([r.meta.obj_id for r in _flat(store.upsert_calls)], [2])
        self.assertEqual(await deps.vector_sync.get_cursor("UserRequest"), newest)

    async def test_a_failing_object_does_not_take_the_next_ones_embeddings(self):
        # The page's embeddings are one shared iterator: the failed object's
        # vectors must still be consumed, or object 2 gets object 1's.
        store = FakeChunkStore()
        store.upsert_error_ids = {1}
        embedder = _embedder_mock()
        embedder.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1), _record(2)]), embedder)

        written = _flat(store.upsert_calls)
        self.assertEqual([r.meta.obj_id for r in written], [2])
        self.assertEqual(written[0].embedding, [0.5, 0.6, 0.7, 0.8])

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
        self.assertEqual(updated[0].filters["status"], "closed")

    async def test_last_update_reaches_the_chunk(self):
        # The date the "solved within the last year" filter runs on — it comes
        # from the source and used to stop at the indexer (TASK-004)
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        self.assertEqual(_flat(store.upsert_calls)[0].meta.updated_at, _NOW)

    async def test_a_moved_last_update_alone_updates_metadata(self):
        # The reopen-and-close-again case: same text, newer date. Without the
        # rewrite the window filter would answer from the first indexing forever
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        later = _NOW + timedelta(days=30)
        embedder2 = _embedder_mock()
        report = await self._run(_deps_mock(store=store2), FakeTicketSource([_record(1, updated_at=later)]), embedder2)

        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_metadata_updated, 1)
        embedder2.embed.assert_not_awaited()
        self.assertEqual(_flat(store2.update_metadata_calls)[0].updated_at, later)

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
        cfg.families["tickets"].classes["UserRequest"].chunks = {
            "body": ChunkFragmentConfig(fields=["description"]),
            "solution": ChunkFragmentConfig(fields=["resolution"]),
        }
        original = VectorRecord(
            obj_id=1,
            index_value="resolved",
            updated_at=_NOW,
            created_at=_NOW - timedelta(days=1),
            payload={"body": "Broken.", "solution": "Rebooted it."},
        )
        store = FakeChunkStore()
        await self._run(_deps_mock(vector_cfg=cfg, store=store), FakeTicketSource([original]))

        store2 = FakeChunkStore()
        store2.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        reopened = VectorRecord(
            obj_id=1,
            index_value="closed",
            updated_at=_NOW,
            created_at=_NOW - timedelta(days=1),
            payload={"body": "Still broken.", "solution": "Rebooted it."},
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


class TestCreationDate(IndexerTestCase):
    """`created_at` belongs to the object, so every chunk of it must carry the
    same value — and a source that has no creation date must not get a fresh
    one on every rewrite (TASK-020)."""

    @staticmethod
    def _carry_over(store: FakeChunkStore) -> FakeChunkStore:
        """A second pass against what the first one wrote."""
        nxt = FakeChunkStore()
        nxt.digests[(_FAMILY, "UserRequest", 1)] = dict(store.digests[(_FAMILY, "UserRequest", 1)])
        return nxt

    async def test_the_sources_own_date_wins(self):
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1)]))

        self.assertEqual(_flat(store.upsert_calls)[0].meta.created_at, _NOW - timedelta(days=1))

    async def test_a_dateless_object_keeps_its_first_creation_date(self):
        # Both dates absent, so the first pass falls back to its own clock —
        # the value that used to be recomputed on every rewrite.
        dateless = _record(1, created_at=None, updated_at=None, description="Broken.")
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([dateless]))
        frozen = _flat(store.upsert_calls)[0].meta.created_at

        store2 = self._carry_over(store)
        await self._run(
            _deps_mock(store=store2),
            FakeTicketSource([_record(1, created_at=None, updated_at=None, description="Still broken.")]),
        )

        self.assertEqual(_flat(store2.upsert_calls)[0].meta.created_at, frozen)

    async def test_a_dateless_object_does_not_churn_on_an_idle_pass(self):
        # The reason `created_at` stayed out of `meta_hash` before the freeze:
        # a moving fallback would rewrite every payload on every sweep.
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1, created_at=None, updated_at=None)]))

        store2 = self._carry_over(store)
        report = await self._run(
            _deps_mock(store=store2), FakeTicketSource([_record(1, created_at=None, updated_at=None)])
        )

        self.assertEqual((report.chunks_embedded, report.chunks_metadata_updated), (0, 0))
        self.assertEqual(store2.update_metadata_calls, [])

    async def test_a_chunk_added_later_inherits_the_objects_date(self):
        # The case per-chunk resolution could not get right: a new chunk has
        # nothing stored of its own and would take the current clock while its
        # siblings keep the old one.
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets"].classes["UserRequest"].chunks = {
            "body": ChunkFragmentConfig(fields=["description"]),
            "solution": ChunkFragmentConfig(fields=["resolution"]),
        }
        store = FakeChunkStore()
        await self._run(
            _deps_mock(vector_cfg=cfg, store=store),
            FakeTicketSource([_record(1, created_at=None, updated_at=None, payload={"body": "Broken."})]),
        )
        frozen = _flat(store.upsert_calls)[0].meta.created_at

        store2 = self._carry_over(store)
        resolved = _record(1, created_at=None, updated_at=None, payload={"body": "Broken.", "solution": "Rebooted it."})
        await self._run(_deps_mock(vector_cfg=cfg, store=store2), FakeTicketSource([resolved]))

        written = _flat(store2.upsert_calls)
        self.assertEqual([c.meta.chunk_kind for c in written], ["solution"])
        self.assertEqual(written[0].meta.created_at, frozen)

    async def test_a_date_arriving_late_replaces_the_frozen_one(self):
        # Mapping `created_at` where it was absent must reach the index —
        # this is what putting the field into `meta_hash` buys.
        store = FakeChunkStore()
        await self._run(_deps_mock(store=store), FakeTicketSource([_record(1, created_at=None, updated_at=None)]))

        store2 = self._carry_over(store)
        real = _NOW - timedelta(days=400)
        report = await self._run(
            _deps_mock(store=store2), FakeTicketSource([_record(1, created_at=real, updated_at=None)])
        )

        self.assertEqual((report.chunks_embedded, report.chunks_metadata_updated), (0, 1))
        self.assertEqual(_flat(store2.update_metadata_calls)[0].created_at, real)


class TestFamilyPacing(IndexerTestCase):
    """TASK-021: a family with its own `sweep_interval_seconds` compares
    against its own last-swept timestamp instead of running on every tick —
    distinct from the per-class cursor, which tracks progress within a pass
    the family was already included in."""

    async def test_family_within_its_own_interval_is_skipped(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets"].sweep_interval_seconds = 3600
        deps = _deps_mock(vector_cfg=cfg)
        await deps.vector_sync.set_reconcile(datetime.now(UTC))  # isolate the sweep phase
        await deps.vector_sync.set_family_swept("tickets", datetime.now(UTC))
        source = FakeTicketSource([_record(1)])

        report = await self._run(deps, source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.objects_seen, 0)
        self.assertEqual(source.prepare_calls, 0)

    async def test_family_past_its_own_interval_runs(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets"].sweep_interval_seconds = 60
        deps = _deps_mock(vector_cfg=cfg)
        await deps.vector_sync.set_reconcile(datetime.now(UTC))
        await deps.vector_sync.set_family_swept("tickets", datetime.now(UTC) - timedelta(hours=1))
        source = FakeTicketSource([_record(1)])

        report = await self._run(deps, source)

        self.assertEqual(report.objects_seen, 1)
        self.assertEqual(source.prepare_calls, 1)

    async def test_a_family_without_its_own_interval_is_never_paced_out(self):
        # No override on the family — pacing must not double up on the
        # scheduler's own tick cadence (already gated on the same
        # system-wide interval), or an out-of-band tick ("Index now", or the
        # scheduler firing a hair early) silently returns zero objects even
        # though nothing about this family was ever slowed down on purpose.
        deps = _deps_mock()
        await deps.vector_sync.set_reconcile(datetime.now(UTC))
        await deps.vector_sync.set_family_swept("tickets", datetime.now(UTC))
        source = FakeTicketSource([_record(1)])

        report = await self._run(deps, source)

        self.assertEqual(report.objects_seen, 1)
        self.assertEqual(source.prepare_calls, 1)

    async def test_a_family_never_swept_before_is_not_skipped(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets"].sweep_interval_seconds = 3600
        deps = _deps_mock(vector_cfg=cfg)
        await deps.vector_sync.set_reconcile(datetime.now(UTC))
        source = FakeTicketSource([_record(1)])

        report = await self._run(deps, source)

        self.assertEqual(report.objects_seen, 1)

    async def test_backfill_ignores_family_pacing(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.families["tickets"].sweep_interval_seconds = 3600
        deps = _deps_mock(vector_cfg=cfg)
        await deps.vector_sync.set_reconcile(datetime.now(UTC))
        await deps.vector_sync.set_family_swept("tickets", datetime.now(UTC))
        await deps.vector_sync.request_reindex()
        source = FakeTicketSource([_record(1)])

        report = await self._run(deps, source)

        self.assertEqual(report.kind, "backfill")
        self.assertEqual(report.objects_seen, 1)

    async def test_family_swept_timestamp_is_set_after_a_successful_pass(self):
        deps = _deps_mock()
        await deps.vector_sync.set_reconcile(datetime.now(UTC))

        await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertIsNotNone(await deps.vector_sync.get_family_swept("tickets"))

    async def test_family_swept_timestamp_is_not_set_on_fingerprint_mismatch(self):
        store = FakeChunkStore()
        store.ensure_version_error = FingerprintMismatchError("dim changed")
        deps = _deps_mock(store=store)

        await self._run(deps, FakeTicketSource([_record(1)]))

        self.assertIsNone(await deps.vector_sync.get_family_swept("tickets"))


class TestSweepIsCounted(IndexerTestCase):
    """REQ-009 R3 asks for sync passes; passes are a timer ticking and answer
    nothing. What the daily document carries is the work — how much of the
    customer's iTop the layer keeps embedded.

    Driven through `sweep_once`, not `tick`: the timer is not the only caller.
    The backfill CLI (`vector/use_cases/reindex.py`) calls this method
    directly, and it is the largest embedding workload an installation runs.
    """

    async def test_a_pass_counts_what_it_embedded(self):
        deps = _deps_mock(store=FakeChunkStore())
        indexer = _indexer(deps, [FakeTicketSource([_record(1)])])

        with patch("itop_ai_assistant.vector.use_cases.indexer.EmbeddingsClient", return_value=_embedder_mock()):
            await indexer.sweep_once()

        counted = await deps.counters.read(datetime.now(UTC).date())
        self.assertEqual(1, counted[Counter.VECTOR_CHUNKS_EMBEDDED])

    async def test_a_pass_that_embedded_nothing_leaves_no_trace(self):
        deps = _deps_mock(configured=False)
        indexer = _indexer(deps, [FakeTicketSource([_record(1)])])

        await indexer.tick()

        counted = await deps.counters.read(datetime.now(UTC).date())
        self.assertEqual(0, counted[Counter.VECTOR_CHUNKS_EMBEDDED])


class TestSweepRegistration(unittest.IsolatedAsyncioTestCase):
    """Infrastructure, not a module: the sweep takes pacing from the scheduler
    and claims no trigger route."""

    async def test_registers_a_loop_when_the_database_is_configured(self):
        tasks = PeriodicTasks()
        _register_sweep(tasks, _deps_mock())

        self.assertEqual(tasks.names, [SWEEP_TASK])

    async def test_no_loop_without_a_database(self):
        tasks = PeriodicTasks()
        _register_sweep(tasks, _deps_mock(configured=False))

        self.assertEqual(tasks.names, [])

    async def test_interval_comes_from_the_runtime_config(self):
        tasks = PeriodicTasks()
        deps = _deps_mock(vector_cfg=_VECTOR_CFG.model_copy(update={"sweep_interval_seconds": 42}))
        _register_sweep(tasks, deps)

        self.assertEqual(await tasks._entries[SWEEP_TASK].interval(), 42)


class TestReindex(IndexerTestCase):
    """The pending backfill lives in Redis, not in the sweeping process: the
    request survives a restart and is honoured by whichever replica wins the
    sweep lock."""

    async def test_request_reindex_marks_it_in_the_index(self):
        deps = _deps_mock()
        indexer = _indexer(deps)
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


class TestIndexerSignature(unittest.TestCase):
    """VectorIndexer/register_vector_sweep take the sweep's dependencies as
    explicit parameters (TASK-039) — pinned the same way
    test_pipelines_ports.py pins the run core's ports. A disjoint handful like
    this earns no protocol, which is exactly why the names are pinned here.
    """

    _EXPECTED = {
        "config_store",
        "vector_sources",
        "vector_store",
        "vector_sync",
        "vector_journal",
        "counters",
    }

    def test_vector_indexer_init_takes_them_by_name(self) -> None:
        params = set(inspect.signature(VectorIndexer.__init__).parameters) - {"self", "sources"}
        self.assertEqual(self._EXPECTED, params)

    def test_register_vector_sweep_takes_them_by_name(self) -> None:
        params = set(inspect.signature(register_vector_sweep).parameters) - {"tasks"}
        self.assertEqual(self._EXPECTED, params)


if __name__ == "__main__":
    unittest.main()
