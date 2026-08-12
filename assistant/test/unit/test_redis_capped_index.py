import unittest

import fakeredis.aioredis

from itop_ai_assistant.util.redis_capped_index import CappedIndex


def _make_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class TestCappedIndex(unittest.IsolatedAsyncioTestCase):
    async def _record(
        self, redis: fakeredis.aioredis.FakeRedis, index: CappedIndex, entry_id: str, when: float
    ) -> None:
        async with redis.pipeline(transaction=True) as pipe:
            index.record(pipe, entry_id, when)
            await pipe.execute()

    async def test_recent_ids_newest_first(self):
        redis = _make_redis()
        index = CappedIndex(redis, "test:index", max_entries=10)
        await self._record(redis, index, "a", 1.0)
        await self._record(redis, index, "b", 2.0)

        self.assertEqual(await index.recent_ids(10), ["b", "a"])

    async def test_recent_ids_respects_limit(self):
        redis = _make_redis()
        index = CappedIndex(redis, "test:index", max_entries=10)
        for i in range(5):
            await self._record(redis, index, str(i), float(i))

        self.assertEqual(len(await index.recent_ids(2)), 2)

    async def test_cap_evicts_oldest(self):
        redis = _make_redis()
        index = CappedIndex(redis, "test:index", max_entries=3)
        for i in range(5):
            await self._record(redis, index, str(i), float(i))

        self.assertEqual(await index.recent_ids(10), ["4", "3", "2"])

    async def test_prune_removes_stale_ids(self):
        redis = _make_redis()
        index = CappedIndex(redis, "test:index", max_entries=10)
        await self._record(redis, index, "a", 1.0)
        await self._record(redis, index, "b", 2.0)

        await index.prune(["a"])

        self.assertEqual(await index.recent_ids(10), ["b"])

    async def test_prune_empty_list_is_a_noop(self):
        redis = _make_redis()
        index = CappedIndex(redis, "test:index", max_entries=10)
        await self._record(redis, index, "a", 1.0)

        await index.prune([])

        self.assertEqual(await index.recent_ids(10), ["a"])
