"""The day's counters: what they hold, what they survive, what they refuse.

The write half is non-fatal and the read half is not — the same split as the
run journal, and for a sharper reason here: this one is incremented on the hot
path of every run.
"""

import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import fakeredis
from redis.exceptions import ConnectionError as RedisConnectionError

from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.util.redis_keyspace import TELEMETRY_COUNTERS_PREFIX, TELEMETRY_COUNTERS_TTL_DAYS


def _today() -> date:
    return datetime.now(UTC).date()


class CountersTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.counters = DailyCounters(self.redis)


class TestCounting(CountersTestCase):
    async def test_increments_accumulate_within_the_day(self):
        await self.counters.bump(Counter.RUNS_WEBHOOK)
        await self.counters.bump(Counter.RUNS_WEBHOOK)
        await self.counters.bump(Counter.LLM_TOKENS_IN, 1500)

        counters = await self.counters.read(_today())

        self.assertEqual(2, counters[Counter.RUNS_WEBHOOK])
        self.assertEqual(1500, counters[Counter.LLM_TOKENS_IN])

    async def test_a_day_is_read_back_whole_with_zeros_for_the_rest(self):
        await self.counters.bump(Counter.ITOP_PUBLIC_COMMENT)

        counters = await self.counters.read(_today())

        self.assertEqual(set(Counter), set(counters))
        self.assertEqual(0, counters[Counter.RUNS_FAILED])

    async def test_days_are_kept_apart(self):
        await self.counters.bump(Counter.RUNS_WEBHOOK)

        yesterday = await self.counters.read(_today() - timedelta(days=1))

        self.assertEqual(0, yesterday[Counter.RUNS_WEBHOOK])

    async def test_the_day_expires_on_its_own(self):
        await self.counters.bump(Counter.RUNS_WEBHOOK)

        ttl = await self.redis.ttl(f"{TELEMETRY_COUNTERS_PREFIX}{_today().isoformat()}")

        self.assertEqual(TELEMETRY_COUNTERS_TTL_DAYS * 24 * 60 * 60, ttl)

    async def test_counters_that_move_together_take_one_round_trip(self):
        """What the model handler needs: the call and both token counts in one
        transaction rather than three (`core/llm_counters.py`)."""
        await self.counters.bump_many({Counter.LLM_CALLS: 1, Counter.LLM_TOKENS_IN: 120, Counter.LLM_TOKENS_OUT: 0})

        counters = await self.counters.read(_today())

        self.assertEqual(1, counters[Counter.LLM_CALLS])
        self.assertEqual(120, counters[Counter.LLM_TOKENS_IN])
        self.assertEqual(0, counters[Counter.LLM_TOKENS_OUT])

    async def test_nothing_is_written_for_a_zero(self):
        """A model that reported no token usage must not be the reason a day
        exists in Redis."""
        await self.counters.bump(Counter.LLM_TOKENS_OUT, 0)

        self.assertEqual([], await self.redis.keys(f"{TELEMETRY_COUNTERS_PREFIX}*"))


class TestRedisOutage(CountersTestCase):
    async def test_an_increment_that_cannot_be_stored_is_not_an_error(self):
        with patch.object(self.redis, "pipeline", side_effect=RedisConnectionError("redis is down")):
            await self.counters.bump(Counter.RUNS_WEBHOOK)

    async def test_a_read_that_cannot_be_served_is(self):
        """Counters are read by the document builder, which must not hand out
        a day of zeros as if that were the answer."""
        with patch.object(self.redis, "hgetall", AsyncMock(side_effect=RedisConnectionError("redis is down"))):
            with self.assertRaises(RedisConnectionError):
                await self.counters.read(_today())


class TestNames(CountersTestCase):
    async def test_a_field_this_version_cannot_name_is_ignored_not_raised(self):
        """Counters outlive the version that wrote them: a renamed counter must
        cost a warning, not the whole day's document."""
        await self.redis.hset(f"{TELEMETRY_COUNTERS_PREFIX}{_today().isoformat()}", "runs_from_the_future", "7")

        counters = await self.counters.read(_today())

        self.assertEqual(set(Counter), set(counters))

    def test_every_trigger_kind_has_a_counter(self):
        for kind in ("webhook", "request", "schedule"):
            with self.subTest(kind=kind):
                self.assertIn(Counter.for_trigger(kind), set(Counter))


if __name__ == "__main__":
    unittest.main()
