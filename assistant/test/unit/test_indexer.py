import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from itop_ai_assistant.config import EmbeddingsConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.vector.chunker import chunk_object
from itop_ai_assistant.vector.index import RECONCILE_SENTINEL, FingerprintMismatchError, IndexMeta
from itop_ai_assistant.vector.indexer import SWEEP_TASK, VectorIndexer, register_vector_sweep
from itop_ai_assistant.vector.source import VectorRecord

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_META = IndexMeta(version=1, model="test-model", dim=4)

_VECTOR_CFG = VectorConfig(
    enabled=True,
    classes={"UserRequest": VectorClassConfig(index_values=["resolved", "closed"], profile={"body": ["description"]})},
    sweep_interval_seconds=300,
    sweep_throttle_seconds=0,
)
_EMB_CFG = EmbeddingsConfig(base_url="http://emb/v1", model="test-model", dimension=4)


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

    def __init__(self, records: list[VectorRecord] | None = None, *, classes: tuple[str, ...] = ("UserRequest",)):
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


def _index_mock(*, locked: bool = True, reindex_pending: bool = False) -> MagicMock:
    index = MagicMock()

    @asynccontextmanager
    async def lock():
        yield locked

    index.try_advisory_lock = lock
    index.ensure_version = AsyncMock(return_value=_META)
    index.get_cursor = AsyncMock(return_value=None)
    index.set_cursor = AsyncMock()
    index.reindex_pending = AsyncMock(return_value=reindex_pending)
    index.request_reindex = AsyncMock()
    index.reset_cursors = AsyncMock()
    index.get_chunk_hashes = AsyncMock(return_value={})
    index.upsert_chunks = AsyncMock(side_effect=lambda records, **kw: len(records))
    index.delete_chunks = AsyncMock(side_effect=lambda cls, oid, keys: len(keys))
    index.delete_object = AsyncMock(return_value=3)
    index.list_object_ids = AsyncMock(return_value=[])
    index.journal_start = AsyncMock(return_value=7)
    index.journal_finish = AsyncMock()
    return index


def _embedder_mock() -> MagicMock:
    embedder = MagicMock()
    embedder.embed = AsyncMock(side_effect=lambda texts: [[0.0] * 4 for _ in texts])
    embedder.aclose = AsyncMock()
    return embedder


def _deps_mock(*, vector_cfg=_VECTOR_CFG, emb_cfg=_EMB_CFG, configured=True) -> MagicMock:
    deps = MagicMock()
    deps.vector_db.configured = configured
    deps.config_store.get = AsyncMock(
        side_effect=lambda name, model: {"vector": vector_cfg, "embeddings": emb_cfg}[name]
    )
    return deps


class IndexerTestCase(unittest.IsolatedAsyncioTestCase):
    async def _run(self, deps, index, source, embedder=None):
        indexer = VectorIndexer(deps, sources=[source])
        self.indexer = indexer
        embedder = embedder or _embedder_mock()
        self.embedder = embedder
        with (
            patch("itop_ai_assistant.vector.indexer.VectorIndex", return_value=index),
            patch("itop_ai_assistant.vector.indexer.EmbeddingsClient", return_value=embedder),
        ):
            return await indexer.sweep_once()


class TestSkips(IndexerTestCase):
    async def test_skip_when_db_not_configured(self):
        report = await self._run(_deps_mock(configured=False), _index_mock(), FakeTicketSource())

        self.assertEqual(report.status, "skipped")
        self.assertIn("database_url", report.skip_reason)

    async def test_skip_when_disabled(self):
        deps = _deps_mock(vector_cfg=VectorConfig(enabled=False))
        report = await self._run(deps, _index_mock(), FakeTicketSource())

        self.assertEqual(report.status, "skipped")
        self.assertIn("disabled", report.skip_reason)

    async def test_skip_when_embeddings_missing(self):
        deps = _deps_mock(emb_cfg=EmbeddingsConfig(base_url=None, model=None))
        report = await self._run(deps, _index_mock(), FakeTicketSource())

        self.assertEqual(report.status, "skipped")
        self.assertIn("embeddings", report.skip_reason)

    async def test_skip_when_lock_not_acquired(self):
        index = _index_mock(locked=False)
        report = await self._run(_deps_mock(), index, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "skipped")
        self.assertIn("lock", report.skip_reason)
        index.journal_start.assert_not_awaited()

    async def test_no_source_registered_for_class_is_reported_as_ok(self):
        # A class in cfg.classes that no registered source claims — logged
        # and skipped, same tolerance as "no chunking profile".
        source = FakeTicketSource([_record(1)], classes=("SomeOtherClass",))
        index = _index_mock()
        report = await self._run(_deps_mock(), index, source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.objects_seen, 0)
        self.assertEqual(source.prepare_calls, 0)


class TestSweep(IndexerTestCase):
    async def test_embeds_and_upserts_new_ticket(self):
        index = _index_mock()
        source = FakeTicketSource([_record(1)])
        report = await self._run(_deps_mock(), index, source)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.kind, "sweep")
        self.assertEqual(report.objects_seen, 1)
        self.assertEqual(report.chunks_embedded, 1)
        self.assertEqual(source.prepare_calls, 1)
        records = index.upsert_chunks.await_args.args[0]
        self.assertEqual(records[0].obj_id, 1)
        self.assertEqual(records[0].chunk_kind, "body")
        self.assertEqual(records[0].status, "resolved")

    async def test_hash_guard_skips_unchanged(self):
        index = _index_mock()
        source = FakeTicketSource([_record(1)])
        await self._run(_deps_mock(), index, source)
        stored = {(r.chunk_kind, r.chunk_n): r.content_hash for r in index.upsert_chunks.await_args.args[0]}

        index2 = _index_mock()
        index2.get_chunk_hashes = AsyncMock(return_value=stored)
        embedder2 = _embedder_mock()
        report = await self._run(_deps_mock(), index2, FakeTicketSource([_record(1)]), embedder2)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 0)
        embedder2.embed.assert_not_awaited()
        index2.upsert_chunks.assert_not_awaited()

    async def test_vanished_chunks_deleted(self):
        index = _index_mock()
        index.get_chunk_hashes = AsyncMock(return_value={("body", 0): "stale", ("body", 5): "gone"})
        report = await self._run(_deps_mock(), index, FakeTicketSource([_record(1)]))

        index.delete_chunks.assert_awaited_once_with("UserRequest", 1, [("body", 5)])
        self.assertEqual(report.chunks_deleted, 1)
        self.assertEqual(report.chunks_embedded, 1)  # ("body", 0) hash mismatch → re-embedded

    async def test_object_out_of_index_values_deleted(self):
        index = _index_mock()
        report = await self._run(_deps_mock(), index, FakeTicketSource([_record(1, index_value="new")]))

        index.delete_object.assert_awaited_once_with("UserRequest", 1)
        self.assertEqual(report.chunks_deleted, 3)
        index.upsert_chunks.assert_not_awaited()

    async def test_empty_index_values_indexes_everything(self):
        cfg = _VECTOR_CFG.model_copy(deep=True)
        cfg.classes["UserRequest"].index_values = []
        index = _index_mock()
        report = await self._run(_deps_mock(vector_cfg=cfg), index, FakeTicketSource([_record(1, index_value="new")]))

        self.assertEqual(report.chunks_embedded, 1)
        index.delete_object.assert_not_awaited()

    async def test_cursor_set_to_max_last_update(self):
        newest = _NOW + timedelta(hours=2)
        index = _index_mock()
        source = FakeTicketSource([_record(1, last_update=_NOW), _record(2, last_update=newest)])
        await self._run(_deps_mock(), index, source)

        class_calls = [c for c in index.set_cursor.await_args_list if c.args[0] == "UserRequest"]
        self.assertEqual(class_calls, [unittest.mock.call("UserRequest", newest)])

    async def test_since_is_cursor_minus_double_interval(self):
        cursor = _NOW

        async def get_cursor(name):
            return cursor if name == "UserRequest" else datetime.now(UTC)  # reconcile not due

        index = _index_mock()
        index.get_cursor = AsyncMock(side_effect=get_cursor)
        source = FakeTicketSource([])
        await self._run(_deps_mock(), index, source)

        since = source.find_modified_since_calls[-1][1]
        self.assertEqual(since, cursor - timedelta(seconds=2 * _VECTOR_CFG.sweep_interval_seconds))

    async def test_class_error_keeps_cursor_and_reports(self):
        index = _index_mock()
        source = FakeTicketSource([])
        source.find_modified_since_error = RuntimeError("itop down")
        report = await self._run(_deps_mock(), index, source)

        self.assertEqual(report.status, "error")
        self.assertIn("itop down", report.errors[0])
        index.set_cursor.assert_not_awaited()
        index.journal_finish.assert_awaited_once()
        self.assertEqual(index.journal_finish.await_args.kwargs["status"], "error")

    async def test_fingerprint_mismatch_is_journaled_error(self):
        index = _index_mock()
        index.ensure_version = AsyncMock(side_effect=FingerprintMismatchError("dim changed"))
        report = await self._run(_deps_mock(), index, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "error")
        self.assertIn("rebuild required", report.errors[0])
        self.assertEqual(index.journal_finish.await_args.kwargs["status"], "error")

    async def test_journal_failure_is_non_fatal(self):
        index = _index_mock()
        index.journal_start = AsyncMock(side_effect=RuntimeError("pg hiccup"))
        report = await self._run(_deps_mock(), index, FakeTicketSource([_record(1)]))

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 1)
        index.journal_finish.assert_not_awaited()


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
    """The pending backfill lives in Postgres, not in the sweeping process:
    the request survives a restart and is honoured by whichever replica wins
    the advisory lock."""

    async def test_request_reindex_marks_it_in_the_index(self):
        index = _index_mock()
        indexer = VectorIndexer(_deps_mock())
        with patch("itop_ai_assistant.vector.indexer.VectorIndex", return_value=index):
            await indexer.request_reindex()

        index.request_reindex.assert_awaited_once()

    async def test_pending_request_resets_cursors_and_runs_backfill(self):
        index = _index_mock(reindex_pending=True)
        report = await self._run(_deps_mock(), index, FakeTicketSource([_record(1)]))

        self.assertEqual(report.kind, "backfill")
        # reset_cursors drops the sentinel row too — that is what clears it
        index.reset_cursors.assert_awaited_once()
        index.journal_start.assert_any_await("backfill")

    async def test_request_survives_a_failed_attempt(self):
        index = _index_mock(reindex_pending=True)
        index.ensure_version = AsyncMock(side_effect=RuntimeError("pg down"))
        await self._run(_deps_mock(), index, FakeTicketSource([_record(1)]))

        # Cursors untouched, so the sentinel stands and the next tick retries
        index.reset_cursors.assert_not_awaited()


class TestReconciliation(IndexerTestCase):
    async def test_due_when_never_ran_and_deletes_orphans(self):
        index = _index_mock()
        index.list_object_ids = AsyncMock(side_effect=lambda cls, after, limit: [1, 2] if after == 0 else [])
        source = FakeTicketSource([])
        source.find_existing_ids_result = {1}
        report = await self._run(_deps_mock(), index, source)

        self.assertEqual(report.status, "ok")
        index.delete_object.assert_awaited_once_with("UserRequest", 2)
        index.journal_start.assert_any_await("reconcile")
        sentinel_call = [c for c in index.set_cursor.await_args_list if c.args[0] == RECONCILE_SENTINEL]
        self.assertEqual(len(sentinel_call), 1)

    async def test_not_due_when_recent(self):
        async def get_cursor(name):
            return datetime.now(UTC) - timedelta(days=1) if name == RECONCILE_SENTINEL else None

        index = _index_mock()
        index.get_cursor = AsyncMock(side_effect=get_cursor)
        report = await self._run(_deps_mock(), index, FakeTicketSource([]))

        self.assertEqual(report.status, "ok")
        index.list_object_ids.assert_not_awaited()
        for call in index.journal_start.await_args_list:
            self.assertNotEqual(call.args[0], "reconcile")

    async def test_skipped_after_class_errors(self):
        index = _index_mock()
        source = FakeTicketSource([])
        source.find_modified_since_error = RuntimeError("boom")
        await self._run(_deps_mock(), index, source)

        index.list_object_ids.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
