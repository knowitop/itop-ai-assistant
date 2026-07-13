import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from config import EmbeddingsConfig, VectorConfig
from domain.ticket import Ticket
from vector.index import RECONCILE_SENTINEL, FingerprintMismatchError, IndexMeta
from vector.indexer import VectorIndexer

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_META = IndexMeta(version=1, model="test-model", dim=4)

_VECTOR_CFG = VectorConfig(
    enabled=True,
    classes=["UserRequest"],
    profiles={"UserRequest": {"body": ["description"]}},
    sweep_interval_seconds=300,
    sweep_throttle_seconds=0,
    index_statuses=["resolved", "closed"],
)
_EMB_CFG = EmbeddingsConfig(base_url="http://emb/v1", model="test-model", dimension=4)


def _ticket(
    obj_id: int, *, status: str = "resolved", description: str = "Broken.", last_update: datetime = _NOW
) -> Ticket:
    return Ticket(
        obj_class="UserRequest",
        id=str(obj_id),
        description=description,
        status=status,
        last_update=last_update,
        created_at=_NOW - timedelta(days=1),
    )


def _index_mock(*, locked: bool = True) -> MagicMock:
    index = MagicMock()

    @asynccontextmanager
    async def lock():
        yield locked

    index.try_advisory_lock = lock
    index.ensure_version = AsyncMock(return_value=_META)
    index.get_cursor = AsyncMock(return_value=None)
    index.set_cursor = AsyncMock()
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


def _deps_mock(
    tickets: list[Ticket] | None = None, *, vector_cfg=_VECTOR_CFG, emb_cfg=_EMB_CFG, configured=True
) -> MagicMock:
    deps = MagicMock()
    deps.vector_db.configured = configured
    deps.config_store.get = AsyncMock(
        side_effect=lambda name, model: {"vector": vector_cfg, "embeddings": emb_cfg}[name]
    )
    bundle = MagicMock()
    pages = [tickets or []]
    bundle.ticket_repo.find_modified_since = AsyncMock(
        side_effect=lambda *a, page, page_size: pages[0] if page == 1 else []
    )
    bundle.ticket_repo.find_existing_ids = AsyncMock(side_effect=lambda cls, ids: set(ids))
    deps.itop.get = AsyncMock(return_value=bundle)
    deps._bundle = bundle
    return deps


class IndexerTestCase(unittest.IsolatedAsyncioTestCase):
    async def _run(self, deps, index, embedder=None):
        indexer = VectorIndexer(deps)
        self.indexer = indexer
        embedder = embedder or _embedder_mock()
        self.embedder = embedder
        with (
            patch("vector.indexer.VectorIndex", return_value=index),
            patch("vector.indexer.EmbeddingsClient", return_value=embedder),
        ):
            return await indexer.sweep_once()


class TestSkips(IndexerTestCase):
    async def test_skip_when_db_not_configured(self):
        report = await self._run(_deps_mock(configured=False), _index_mock())

        self.assertEqual(report.status, "skipped")
        self.assertIn("database_url", report.skip_reason)

    async def test_skip_when_disabled(self):
        deps = _deps_mock(vector_cfg=VectorConfig(enabled=False))
        report = await self._run(deps, _index_mock())

        self.assertEqual(report.status, "skipped")
        self.assertIn("disabled", report.skip_reason)

    async def test_skip_when_embeddings_missing(self):
        deps = _deps_mock(emb_cfg=EmbeddingsConfig(base_url=None, model=None))
        report = await self._run(deps, _index_mock())

        self.assertEqual(report.status, "skipped")
        self.assertIn("embeddings", report.skip_reason)

    async def test_skip_when_lock_not_acquired(self):
        index = _index_mock(locked=False)
        report = await self._run(_deps_mock([_ticket(1)]), index)

        self.assertEqual(report.status, "skipped")
        self.assertIn("lock", report.skip_reason)
        index.journal_start.assert_not_awaited()


class TestSweep(IndexerTestCase):
    async def test_embeds_and_upserts_new_ticket(self):
        index = _index_mock()
        report = await self._run(_deps_mock([_ticket(1)]), index)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.kind, "sweep")
        self.assertEqual(report.objects_seen, 1)
        self.assertEqual(report.chunks_embedded, 1)
        records = index.upsert_chunks.await_args.args[0]
        self.assertEqual(records[0].obj_id, 1)
        self.assertEqual(records[0].chunk_kind, "body")
        self.assertEqual(records[0].status, "resolved")

    async def test_hash_guard_skips_unchanged(self):
        index = _index_mock()
        deps = _deps_mock([_ticket(1)])
        await self._run(deps, index)
        stored = {(r.chunk_kind, r.chunk_n): r.content_hash for r in index.upsert_chunks.await_args.args[0]}

        index2 = _index_mock()
        index2.get_chunk_hashes = AsyncMock(return_value=stored)
        embedder2 = _embedder_mock()
        report = await self._run(deps, index2, embedder2)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 0)
        embedder2.embed.assert_not_awaited()
        index2.upsert_chunks.assert_not_awaited()

    async def test_vanished_chunks_deleted(self):
        index = _index_mock()
        index.get_chunk_hashes = AsyncMock(return_value={("body", 0): "stale", ("body", 5): "gone"})
        report = await self._run(_deps_mock([_ticket(1)]), index)

        index.delete_chunks.assert_awaited_once_with("UserRequest", 1, [("body", 5)])
        self.assertEqual(report.chunks_deleted, 1)
        self.assertEqual(report.chunks_embedded, 1)  # ("body", 0) hash mismatch → re-embedded

    async def test_ticket_out_of_index_statuses_deleted(self):
        index = _index_mock()
        report = await self._run(_deps_mock([_ticket(1, status="new")]), index)

        index.delete_object.assert_awaited_once_with("UserRequest", 1)
        self.assertEqual(report.chunks_deleted, 3)
        index.upsert_chunks.assert_not_awaited()

    async def test_cursor_set_to_max_last_update(self):
        newest = _NOW + timedelta(hours=2)
        index = _index_mock()
        await self._run(_deps_mock([_ticket(1, last_update=_NOW), _ticket(2, last_update=newest)]), index)

        class_calls = [c for c in index.set_cursor.await_args_list if c.args[0] == "UserRequest"]
        self.assertEqual(class_calls, [unittest.mock.call("UserRequest", newest)])

    async def test_since_is_cursor_minus_double_interval(self):
        cursor = _NOW

        async def get_cursor(name):
            return cursor if name == "UserRequest" else datetime.now(UTC)  # reconcile not due

        index = _index_mock()
        index.get_cursor = AsyncMock(side_effect=get_cursor)
        deps = _deps_mock([])
        await self._run(deps, index)

        since = deps._bundle.ticket_repo.find_modified_since.await_args.args[1]
        self.assertEqual(since, cursor - timedelta(seconds=2 * _VECTOR_CFG.sweep_interval_seconds))

    async def test_class_error_keeps_cursor_and_reports(self):
        index = _index_mock()
        deps = _deps_mock()
        deps._bundle.ticket_repo.find_modified_since = AsyncMock(side_effect=RuntimeError("itop down"))
        report = await self._run(deps, index)

        self.assertEqual(report.status, "error")
        self.assertIn("itop down", report.errors[0])
        index.set_cursor.assert_not_awaited()
        index.journal_finish.assert_awaited_once()
        self.assertEqual(index.journal_finish.await_args.kwargs["status"], "error")

    async def test_fingerprint_mismatch_is_journaled_error(self):
        index = _index_mock()
        index.ensure_version = AsyncMock(side_effect=FingerprintMismatchError("dim changed"))
        report = await self._run(_deps_mock([_ticket(1)]), index)

        self.assertEqual(report.status, "error")
        self.assertIn("rebuild required", report.errors[0])
        self.assertEqual(index.journal_finish.await_args.kwargs["status"], "error")

    async def test_journal_failure_is_non_fatal(self):
        index = _index_mock()
        index.journal_start = AsyncMock(side_effect=RuntimeError("pg hiccup"))
        report = await self._run(_deps_mock([_ticket(1)]), index)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.chunks_embedded, 1)
        index.journal_finish.assert_not_awaited()


class TestReindex(IndexerTestCase):
    async def test_request_reindex_resets_cursors_and_runs_backfill(self):
        index = _index_mock()
        deps = _deps_mock([_ticket(1)])
        indexer = VectorIndexer(deps)
        indexer.request_reindex()
        self.assertTrue(indexer._wake.is_set())

        with (
            patch("vector.indexer.VectorIndex", return_value=index),
            patch("vector.indexer.EmbeddingsClient", return_value=_embedder_mock()),
        ):
            report = await indexer.sweep_once()

        self.assertEqual(report.kind, "backfill")
        index.reset_cursors.assert_awaited_once()
        index.journal_start.assert_any_await("backfill")
        self.assertFalse(indexer._full_requested)

    async def test_full_flag_survives_failed_attempt(self):
        index = _index_mock()
        index.ensure_version = AsyncMock(side_effect=RuntimeError("pg down"))
        deps = _deps_mock([_ticket(1)])
        indexer = VectorIndexer(deps)
        indexer.request_reindex()

        with (
            patch("vector.indexer.VectorIndex", return_value=index),
            patch("vector.indexer.EmbeddingsClient", return_value=_embedder_mock()),
        ):
            await indexer.sweep_once()

        index.reset_cursors.assert_not_awaited()
        self.assertTrue(indexer._full_requested)  # next tick retries the backfill


class TestReconciliation(IndexerTestCase):
    async def test_due_when_never_ran_and_deletes_orphans(self):
        index = _index_mock()
        index.list_object_ids = AsyncMock(side_effect=lambda cls, after, limit: [1, 2] if after == 0 else [])
        deps = _deps_mock([])
        deps._bundle.ticket_repo.find_existing_ids = AsyncMock(return_value={1})
        report = await self._run(deps, index)

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
        report = await self._run(_deps_mock([]), index)

        self.assertEqual(report.status, "ok")
        index.list_object_ids.assert_not_awaited()
        for call in index.journal_start.await_args_list:
            self.assertNotEqual(call.args[0], "reconcile")

    async def test_skipped_after_class_errors(self):
        index = _index_mock()
        deps = _deps_mock()
        deps._bundle.ticket_repo.find_modified_since = AsyncMock(side_effect=RuntimeError("boom"))
        await self._run(deps, index)

        index.list_object_ids.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
