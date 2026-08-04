import unittest
from datetime import UTC, datetime

import fakeredis.aioredis

from itop_ai_assistant.vector.sync_state import VectorSyncState

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _make_state() -> tuple[VectorSyncState, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return VectorSyncState(redis), redis


class TestCursors(unittest.IsolatedAsyncioTestCase):
    async def test_unset_cursor_reads_as_none(self):
        state, _ = _make_state()

        self.assertIsNone(await state.get_cursor("UserRequest"))

    async def test_cursor_round_trips_with_timezone(self):
        state, _ = _make_state()

        await state.set_cursor("UserRequest", _NOW)

        self.assertEqual(await state.get_cursor("UserRequest"), _NOW)

    async def test_list_cursors_excludes_bookkeeping_keys(self):
        state, _ = _make_state()
        await state.set_cursor("UserRequest", _NOW)
        await state.set_reconcile(_NOW)
        await state.request_reindex()

        self.assertEqual(await state.list_cursors(), {"UserRequest": _NOW})

    async def test_reset_drops_cursors_and_the_pending_request(self):
        state, _ = _make_state()
        await state.set_cursor("UserRequest", _NOW)
        await state.request_reindex()

        await state.reset_cursors()

        self.assertEqual(await state.list_cursors(), {})
        self.assertFalse(await state.reindex_pending())


class TestReindexRequest(unittest.IsolatedAsyncioTestCase):
    async def test_request_survives_the_process_that_made_it(self):
        # The point of storing it in Redis: whichever replica wins the lock acts on it
        state, redis = _make_state()

        await state.request_reindex()

        self.assertTrue(await VectorSyncState(redis).reindex_pending())

    async def test_requesting_twice_is_idempotent(self):
        state, _ = _make_state()

        await state.request_reindex()
        await state.request_reindex()

        self.assertTrue(await state.reindex_pending())
