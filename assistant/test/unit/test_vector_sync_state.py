import asyncio
import unittest
from datetime import UTC, datetime

import fakeredis.aioredis

from itop_ai_assistant.vector.sync_state import LOCK_TTL_SECONDS, VectorSyncState

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


class TestSweepLock(unittest.IsolatedAsyncioTestCase):
    async def test_lock_is_granted_when_free(self):
        state, _ = _make_state()

        async with state.sweep_lock() as locked:
            self.assertTrue(locked)

    async def test_second_holder_is_refused_without_waiting(self):
        state, redis = _make_state()
        other = VectorSyncState(redis)

        async with state.sweep_lock() as locked:
            self.assertTrue(locked)
            async with other.sweep_lock() as second:
                self.assertFalse(second)

    async def test_lock_is_released_on_exit(self):
        state, redis = _make_state()

        async with state.sweep_lock():
            pass

        async with VectorSyncState(redis).sweep_lock() as locked:
            self.assertTrue(locked)

    async def test_lock_is_released_when_the_body_raises(self):
        state, redis = _make_state()

        with self.assertRaises(RuntimeError):
            async with state.sweep_lock():
                raise RuntimeError("sweep exploded")

        async with VectorSyncState(redis).sweep_lock() as locked:
            self.assertTrue(locked)

    async def test_holder_outlives_the_ttl(self):
        # The reason this lock exists in this shape: a backfill runs for hours
        state, redis = _make_state()

        async with state.sweep_lock(ttl_seconds=1, renew_interval=0.1) as locked:
            self.assertTrue(locked)
            await asyncio.sleep(1.5)
            self.assertGreater(await redis.ttl("vector:sweep:lock"), 0)
            async with VectorSyncState(redis).sweep_lock() as second:
                self.assertFalse(second)

    async def test_a_foreign_lock_is_never_deleted(self):
        state, redis = _make_state()
        await redis.set("vector:sweep:lock", "someone-else", ex=LOCK_TTL_SECONDS)

        async with state.sweep_lock() as locked:
            self.assertFalse(locked)

        self.assertEqual(await redis.get("vector:sweep:lock"), "someone-else")
