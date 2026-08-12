"""Recency-capped Redis sorted-set index.

`state/journal.py` and `vector/state/index_journal.py` each keep a bounded history
of run-shaped entries and used to duplicate the same sequence independently:
`zadd` + `zremrangebyrank` to record and cap, `zrevrange` + prune to read back
and self-heal past entries that expired or were evicted out from under the
index. This is that sequence, factored out; how an entry itself is stored
(hash with its own TTL, JSON blob, ...) stays the owner's business.
"""

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline


class CappedIndex:
    def __init__(self, redis: Redis, index_key: str, max_entries: int):
        self._redis = redis
        self._index_key = index_key
        self._max_entries = max_entries

    def record(self, pipe: Pipeline, entry_id: str, when: float) -> None:
        """Queue the zadd + cap-eviction commands onto a caller-owned pipeline,
        so the index update lands in the same transaction as the entry write."""
        pipe.zadd(self._index_key, {entry_id: when})
        pipe.zremrangebyrank(self._index_key, 0, -self._max_entries - 1)

    async def recent_ids(self, limit: int) -> list[str]:
        """Most recent entry ids first."""
        return await self._redis.zrevrange(self._index_key, 0, limit - 1)

    async def prune(self, stale_ids: list[str]) -> None:
        """Drop ids whose entry is gone — a no-op on an empty list."""
        if stale_ids:
            await self._redis.zrem(self._index_key, *stale_ids)
