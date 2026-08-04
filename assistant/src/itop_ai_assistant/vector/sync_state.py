"""Everything the sweep knows about its own progress — kept in Redis.

Postgres held this before (`vector_sync_state`), which made the vector
backend the owner of state that is not about vectors at all. It is
operational state, and this project keeps operational state in Redis; the
sweep now reads it from the same place as ticket state and the run journal.

No TTL on any of these keys: losing a cursor is not an error but it does
cost a full backfill on a CPU-only box, which ADR-006 measures in hours.
"""

from datetime import datetime

from redis.asyncio import Redis

_PREFIX = "vector:"
_CURSOR_PREFIX = f"{_PREFIX}cursor:"
_RECONCILE_KEY = f"{_PREFIX}reconcile"
_REINDEX_KEY = f"{_PREFIX}reindex"


class VectorSyncState:
    """Sweep cursors, the reconciliation clock and the pending-backfill flag."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _cursor_key(self, obj_class: str) -> str:
        return f"{_CURSOR_PREFIX}{obj_class}"

    async def get_cursor(self, obj_class: str) -> datetime | None:
        return _parse(await self._redis.get(self._cursor_key(obj_class)))

    async def set_cursor(self, obj_class: str, cursor: datetime) -> None:
        await self._redis.set(self._cursor_key(obj_class), cursor.isoformat())

    async def list_cursors(self) -> dict[str, datetime | None]:
        """Per-class cursors only — the reconcile clock and the reindex flag
        live under their own keys and never show up here."""
        cursors: dict[str, datetime | None] = {}
        async for key in self._redis.scan_iter(match=f"{_CURSOR_PREFIX}*"):
            cursors[key.removeprefix(_CURSOR_PREFIX)] = _parse(await self._redis.get(key))
        return cursors

    async def reset_cursors(self) -> None:
        """Drop every cursor and the pending request — the next sweep is a full backfill."""
        keys = [key async for key in self._redis.scan_iter(match=f"{_CURSOR_PREFIX}*")]
        if keys:
            await self._redis.delete(*keys)
        await self._redis.delete(_REINDEX_KEY)

    async def get_reconcile(self) -> datetime | None:
        return _parse(await self._redis.get(_RECONCILE_KEY))

    async def set_reconcile(self, when: datetime) -> None:
        await self._redis.set(_RECONCILE_KEY, when.isoformat())

    async def request_reindex(self) -> None:
        """Mark a full backfill as pending. Idempotent; cleared by reset_cursors."""
        await self._redis.set(_REINDEX_KEY, "1")

    async def reindex_pending(self) -> bool:
        return await self._redis.exists(_REINDEX_KEY) == 1


def _parse(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
