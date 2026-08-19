import unittest
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
from pydantic import BaseModel
from redis.exceptions import RedisError

from itop_ai_assistant.state.ticket_state import StateUnavailableError, TicketStateManager

TTL_30_DAYS = 30 * 24 * 60 * 60


class _Counter(BaseModel):
    """A throwaway model, standing in for any module's own state shape."""

    rounds: int = 0
    ai_done: bool = False


def _make_manager() -> tuple[TicketStateManager, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return TicketStateManager(redis), redis


class TestTicketStateGet(unittest.IsolatedAsyncioTestCase):
    async def test_get_returns_default_when_key_missing(self):
        manager, _ = _make_manager()
        state = await manager.get("probe", "R-000123", _Counter)
        self.assertEqual(state, _Counter(rounds=0, ai_done=False))

    async def test_get_returns_stored_state(self):
        manager, redis = _make_manager()
        await redis.hset("ticket:R-000042:probe", mapping={"rounds": "3", "ai_done": "1"})

        state = await manager.get("probe", "R-000042", _Counter)
        self.assertEqual(state.rounds, 3)
        self.assertTrue(state.ai_done)

    async def test_get_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "hgetall", AsyncMock(side_effect=RedisError("conn refused"))):
            with self.assertRaises(StateUnavailableError):
                await manager.get("probe", "R-000001", _Counter)


class TestTicketStateIncrement(unittest.IsolatedAsyncioTestCase):
    async def test_increment_from_zero(self):
        manager, _ = _make_manager()
        ticket_ref = "R-000010"
        await manager.increment("probe", ticket_ref, "rounds")
        state = await manager.get("probe", ticket_ref, _Counter)
        self.assertEqual(state.rounds, 1)

    async def test_increment_accumulates(self):
        manager, _ = _make_manager()
        ticket_ref = "R-000010"
        await manager.increment("probe", ticket_ref, "rounds")
        await manager.increment("probe", ticket_ref, "rounds")
        await manager.increment("probe", ticket_ref, "rounds")
        state = await manager.get("probe", ticket_ref, _Counter)
        self.assertEqual(state.rounds, 3)

    async def test_increment_sets_ttl(self):
        manager, redis = _make_manager()
        ticket_ref = "R-000010"
        await manager.increment("probe", ticket_ref, "rounds")
        ttl = await redis.ttl("ticket:R-000010:probe")
        self.assertAlmostEqual(ttl, TTL_30_DAYS, delta=5)

    async def test_increment_resets_ttl(self):
        manager, redis = _make_manager()
        await redis.hset("ticket:R-000010:probe", "rounds", "5")
        await redis.expire("ticket:R-000010:probe", 60)  # short TTL

        await manager.increment("probe", "R-000010", "rounds")

        ttl = await redis.ttl("ticket:R-000010:probe")
        self.assertGreater(ttl, 60)

    async def test_increment_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "pipeline", side_effect=RedisError("conn refused")):
            with self.assertRaises(StateUnavailableError):
                await manager.increment("probe", "R-000001", "rounds")


class TestTicketStateLock(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_lock_returns_true_when_free(self):
        manager, _ = _make_manager()
        self.assertTrue(await manager.acquire_lock("R-000001"))

    async def test_acquire_lock_returns_false_when_held(self):
        manager, _ = _make_manager()
        await manager.acquire_lock("R-000001")
        self.assertFalse(await manager.acquire_lock("R-000001"))

    async def test_release_lock_allows_reacquire(self):
        manager, _ = _make_manager()
        await manager.acquire_lock("R-000001")
        await manager.release_lock("R-000001")
        self.assertTrue(await manager.acquire_lock("R-000001"))

    async def test_locks_are_per_ticket(self):
        manager, _ = _make_manager()
        await manager.acquire_lock("R-000001")
        self.assertTrue(await manager.acquire_lock("R-000002"))

    async def test_lock_has_ttl(self):
        manager, redis = _make_manager()
        await manager.acquire_lock("R-000001")
        ttl = await redis.ttl("lock:R-000001")
        self.assertGreater(ttl, 0)

    async def test_acquire_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "set", AsyncMock(side_effect=RedisError("conn refused"))):
            with self.assertRaises(StateUnavailableError):
                await manager.acquire_lock("R-000001")

    async def test_release_swallows_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "delete", AsyncMock(side_effect=RedisError("conn refused"))):
            await manager.release_lock("R-000001")  # must not raise


class TestTicketStateCustomTtl(unittest.IsolatedAsyncioTestCase):
    async def test_custom_ttl_applied_to_state_keys(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        manager = TicketStateManager(redis, ttl_seconds=3600)
        await manager.increment("probe", "R-000001", "rounds")
        ttl = await redis.ttl("ticket:R-000001:probe")
        self.assertAlmostEqual(ttl, 3600, delta=5)


class TestTicketStateSetFlag(unittest.IsolatedAsyncioTestCase):
    async def test_set_flag_sets_field(self):
        manager, _ = _make_manager()
        await manager.set_flag("probe", "R-000007", "ai_done")
        state = await manager.get("probe", "R-000007", _Counter)
        self.assertTrue(state.ai_done)

    async def test_set_flag_preserves_other_fields(self):
        manager, redis = _make_manager()
        await redis.hset("ticket:R-000007:probe", mapping={"rounds": "2", "ai_done": "0"})

        await manager.set_flag("probe", "R-000007", "ai_done")

        state = await manager.get("probe", "R-000007", _Counter)
        self.assertTrue(state.ai_done)
        self.assertEqual(state.rounds, 2)

    async def test_set_flag_sets_ttl(self):
        manager, redis = _make_manager()
        await manager.set_flag("probe", "R-000007", "ai_done")
        ttl = await redis.ttl("ticket:R-000007:probe")
        self.assertAlmostEqual(ttl, TTL_30_DAYS, delta=5)

    async def test_set_flag_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "pipeline", side_effect=RedisError("conn refused")):
            with self.assertRaises(StateUnavailableError):
                await manager.set_flag("probe", "R-000001", "ai_done")


class TestTicketStateIsolatedPerModule(unittest.IsolatedAsyncioTestCase):
    """The actual point of the generic API (TASK-047): two modules never see
    each other's fields, even on the same ticket — the lock is the one thing
    they genuinely share."""

    async def test_counters_do_not_leak_across_modules(self):
        manager, _ = _make_manager()
        ticket_ref = "R-000099"
        await manager.increment("a", ticket_ref, "rounds")

        state_a = await manager.get("a", ticket_ref, _Counter)
        state_b = await manager.get("b", ticket_ref, _Counter)

        self.assertEqual(state_a.rounds, 1)
        self.assertEqual(state_b.rounds, 0)

    async def test_lock_is_shared_across_modules(self):
        manager, _ = _make_manager()
        ticket_ref = "R-000099"
        self.assertTrue(await manager.acquire_lock(ticket_ref))
        self.assertFalse(await manager.acquire_lock(ticket_ref))


if __name__ == "__main__":
    unittest.main()
