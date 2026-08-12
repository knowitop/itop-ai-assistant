"""Everything the sweep knows about its own progress — kept in Redis.

The relational vector store held this before (`vector_sync_state`), which
made the vector backend the owner of state that is not about vectors at
all. It is operational state, and this project keeps operational state in
Redis; the sweep now reads it from the same place as ticket state and the
run journal.

No TTL on any of these keys: losing a cursor is not an error but it does
cost a full backfill on a CPU-only box, which ADR-006 measures in hours.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from redis.asyncio import Redis

from itop_ai_assistant.util.redis_keyspace import (
    VECTOR_CURSOR_PREFIX,
    VECTOR_FAMILY_SWEPT_PREFIX,
    VECTOR_RECONCILE_KEY,
    VECTOR_REINDEX_KEY,
    VECTOR_SWEEP_LOCK_KEY,
)
from itop_ai_assistant.util.redis_keyspace import (
    VECTOR_SWEEP_LOCK_RENEW_INTERVAL_SECONDS as RENEW_INTERVAL_SECONDS,
)
from itop_ai_assistant.util.redis_keyspace import (
    VECTOR_SWEEP_LOCK_TTL_SECONDS as LOCK_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


class VectorSyncState:
    """Sweep cursors, the reconciliation clock and the pending-backfill flag."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _cursor_key(self, obj_class: str) -> str:
        return f"{VECTOR_CURSOR_PREFIX}{obj_class}"

    async def get_cursor(self, obj_class: str) -> datetime | None:
        return _parse(await self._redis.get(self._cursor_key(obj_class)))

    async def set_cursor(self, obj_class: str, cursor: datetime) -> None:
        await self._redis.set(self._cursor_key(obj_class), cursor.isoformat())

    async def list_cursors(self) -> dict[str, datetime | None]:
        """Per-class cursors only — the reconcile clock and the reindex flag
        live under their own keys and never show up here."""
        cursors: dict[str, datetime | None] = {}
        async for key in self._redis.scan_iter(match=f"{VECTOR_CURSOR_PREFIX}*"):
            cursors[key.removeprefix(VECTOR_CURSOR_PREFIX)] = _parse(await self._redis.get(key))
        return cursors

    async def reset_cursors(self) -> None:
        """Drop every cursor and the pending request — the next sweep is a full backfill."""
        keys = [key async for key in self._redis.scan_iter(match=f"{VECTOR_CURSOR_PREFIX}*")]
        if keys:
            await self._redis.delete(*keys)
        await self._redis.delete(VECTOR_REINDEX_KEY)

    def _family_swept_key(self, family: str) -> str:
        return f"{VECTOR_FAMILY_SWEPT_PREFIX}{family}"

    async def get_family_swept(self, family: str) -> datetime | None:
        """When this family's classes were last actually included in a sweep
        pass — distinct from the per-class cursor, which tracks incremental
        progress *within* a pass. Drives per-family pacing (TASK-021): a
        family with its own `sweep_interval_seconds` compares against this
        instead of running on every tick."""
        return _parse(await self._redis.get(self._family_swept_key(family)))

    async def set_family_swept(self, family: str, when: datetime) -> None:
        await self._redis.set(self._family_swept_key(family), when.isoformat())

    async def get_reconcile(self) -> datetime | None:
        return _parse(await self._redis.get(VECTOR_RECONCILE_KEY))

    async def set_reconcile(self, when: datetime) -> None:
        await self._redis.set(VECTOR_RECONCILE_KEY, when.isoformat())

    async def request_reindex(self) -> None:
        """Mark a full backfill as pending. Idempotent; cleared by reset_cursors."""
        await self._redis.set(VECTOR_REINDEX_KEY, "1")

    async def reindex_pending(self) -> bool:
        return await self._redis.exists(VECTOR_REINDEX_KEY) == 1

    @asynccontextmanager
    async def sweep_lock(
        self, *, ttl_seconds: int = LOCK_TTL_SECONDS, renew_interval: float = RENEW_INTERVAL_SECONDS
    ) -> AsyncIterator[bool]:
        """Cross-replica exclusion for the sweep. Yields False immediately
        when someone else holds it — the sweep skips rather than queues.

        The lock is renewed from a background task for as long as the body
        runs, because a full backfill takes hours on CPU-only embeddings
        while the TTL has to stay short enough to survive a crashed replica.
        Renewal is a read-then-extend rather than one atomic script: losing
        the key between the two calls needs it to expire *and* be re-taken
        inside the same tick, which a 120s TTL renewed every 40s does not
        offer. The cost of that race is a duplicated sweep pass, and the
        hash-guard makes a duplicated pass cheap.
        """
        token = str(uuid4())
        acquired = bool(await self._redis.set(VECTOR_SWEEP_LOCK_KEY, token, nx=True, ex=ttl_seconds))
        if not acquired:
            yield False
            return
        renewer = asyncio.create_task(self._renew(token, ttl_seconds, renew_interval))
        try:
            yield True
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
            # Compare before deleting: never drop a lock that is no longer ours
            if await self._redis.get(VECTOR_SWEEP_LOCK_KEY) == token:
                await self._redis.delete(VECTOR_SWEEP_LOCK_KEY)

    async def _renew(self, token: str, ttl_seconds: int, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                if await self._redis.get(VECTOR_SWEEP_LOCK_KEY) != token:
                    logger.warning("vector sweep lock was taken over — this pass no longer holds it")
                    return
                await self._redis.expire(VECTOR_SWEEP_LOCK_KEY, ttl_seconds)
            except Exception as e:  # Redis blip: keep going, the TTL still has room
                logger.warning(f"vector sweep lock renewal failed (will retry): {e}")


def _parse(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
