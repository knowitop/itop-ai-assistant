import unittest
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
from redis.exceptions import RedisError

from state.ticket_state import StateUnavailableError, TicketState, TicketStateManager

TTL_30_DAYS = 30 * 24 * 60 * 60


def _make_manager() -> tuple[TicketStateManager, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return TicketStateManager(redis), redis


class TestTicketStateGet(unittest.IsolatedAsyncioTestCase):
    async def test_get_returns_default_when_key_missing(self):
        manager, _ = _make_manager()
        state = await manager.get("R-000123")
        self.assertEqual(state, TicketState(rounds=0, ai_done=False))

    async def test_get_returns_stored_state(self):
        manager, redis = _make_manager()
        await redis.hset("ticket:R-000042", mapping={"rounds": "3", "ai_done": "1"})

        state = await manager.get("R-000042")
        self.assertEqual(state.rounds, 3)
        self.assertTrue(state.ai_done)

    async def test_get_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "hgetall", AsyncMock(side_effect=RedisError("conn refused"))):
            with self.assertRaises(StateUnavailableError):
                await manager.get("R-000001")


class TestTicketStateIncrementRounds(unittest.IsolatedAsyncioTestCase):
    async def test_increment_rounds_from_zero(self):
        manager, _ = _make_manager()
        ticket_ref = "R-000010"
        await manager.increment_rounds(ticket_ref)
        state = await manager.get(ticket_ref)
        self.assertEqual(state.rounds, 1)

    async def test_increment_rounds_accumulates(self):
        manager, _ = _make_manager()
        ticket_ref = "R-000010"
        await manager.increment_rounds(ticket_ref)
        await manager.increment_rounds(ticket_ref)
        await manager.increment_rounds(ticket_ref)
        state = await manager.get(ticket_ref)
        self.assertEqual(state.rounds, 3)

    async def test_increment_rounds_sets_ttl(self):
        manager, redis = _make_manager()
        ticket_ref = "R-000010"
        await manager.increment_rounds(ticket_ref)
        ttl = await redis.ttl("ticket:R-000010")
        self.assertAlmostEqual(ttl, TTL_30_DAYS, delta=5)

    async def test_increment_rounds_resets_ttl(self):
        manager, redis = _make_manager()
        await redis.hset("ticket:R-000010", "rounds", "5")
        await redis.expire("ticket:R-000010", 60)  # short TTL

        await manager.increment_rounds("R-000010")

        ttl = await redis.ttl("ticket:R-000010")
        self.assertGreater(ttl, 60)

    async def test_increment_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "pipeline", side_effect=RedisError("conn refused")):
            with self.assertRaises(StateUnavailableError):
                await manager.increment_rounds(1)


class TestTicketStateMarkDone(unittest.IsolatedAsyncioTestCase):
    async def test_mark_done_sets_flag(self):
        manager, _ = _make_manager()
        await manager.mark_done("R-000007")
        state = await manager.get("R-000007")
        self.assertTrue(state.ai_done)

    async def test_mark_done_preserves_rounds(self):
        manager, redis = _make_manager()
        await redis.hset("ticket:R-000007", mapping={"rounds": "2", "ai_done": "0"})

        await manager.mark_done("R-000007")

        state = await manager.get("R-000007")
        self.assertTrue(state.ai_done)
        self.assertEqual(state.rounds, 2)

    async def test_mark_done_sets_ttl(self):
        manager, redis = _make_manager()
        await manager.mark_done("R-000007")
        ttl = await redis.ttl("ticket:R-000007")
        self.assertAlmostEqual(ttl, TTL_30_DAYS, delta=5)

    async def test_mark_done_raises_on_redis_error(self):
        manager, _ = _make_manager()
        with patch.object(manager._redis, "pipeline", side_effect=RedisError("conn refused")):
            with self.assertRaises(StateUnavailableError):
                await manager.mark_done("R-000001")


if __name__ == "__main__":
    unittest.main()
