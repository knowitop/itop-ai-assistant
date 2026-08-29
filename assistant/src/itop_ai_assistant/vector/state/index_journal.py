"""History of indexing runs — the same shape as `state/journal.py`, one hash per
entry plus a capped sorted index.

Journal writes are observability, never correctness: the sweep treats a
failure here as a warning (see `vector/use_cases/indexer.py`), and finishing an entry
that has already been evicted is a no-op rather than an error.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from redis.asyncio import Redis

from itop_ai_assistant.util.redis_capped_index import CappedIndex
from itop_ai_assistant.util.redis_keyspace import (
    VECTOR_RUN_INDEX_KEY,
    VECTOR_RUN_PREFIX,
)
from itop_ai_assistant.util.redis_keyspace import (
    VECTOR_RUN_INDEX_MAX_ENTRIES as MAX_ENTRIES,
)

_COUNTERS = ("objects_seen", "objects_skipped", "chunks_embedded", "chunks_metadata_updated", "chunks_deleted")


class IndexJournal:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._index = CappedIndex(redis, VECTOR_RUN_INDEX_KEY, MAX_ENTRIES)

    def _key(self, entry_id: str) -> str:
        return f"{VECTOR_RUN_PREFIX}{entry_id}"

    async def start(self, kind: str) -> str:
        entry_id = str(uuid4())
        now = datetime.now(UTC)
        entry = {
            "id": entry_id,
            "kind": kind,
            "status": "running",
            "started_at": now.isoformat(),
            "finished_at": None,
            "error": None,
            # What the pass chose not to index, as opposed to what broke it:
            # present whatever the status is, and never sets the status itself.
            "warning": None,
            **{counter: 0 for counter in _COUNTERS},
        }
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._key(entry_id), json.dumps(entry))
            self._index.record(pipe, entry_id, now.timestamp())
            await pipe.execute()
        return entry_id

    async def finish(
        self,
        entry_id: str,
        *,
        status: str,
        objects_seen: int = 0,
        objects_skipped: int = 0,
        chunks_embedded: int = 0,
        chunks_metadata_updated: int = 0,
        chunks_deleted: int = 0,
        error: str | None = None,
        warning: str | None = None,
    ) -> None:
        raw = await self._redis.get(self._key(entry_id))
        if raw is None:  # evicted by the cap while the run was going
            return
        entry = json.loads(raw)
        entry.update(
            status=status,
            finished_at=datetime.now(UTC).isoformat(),
            objects_seen=objects_seen,
            objects_skipped=objects_skipped,
            chunks_embedded=chunks_embedded,
            chunks_metadata_updated=chunks_metadata_updated,
            chunks_deleted=chunks_deleted,
            error=error,
            warning=warning,
        )
        await self._redis.set(self._key(entry_id), json.dumps(entry))

    async def recent(self, limit: int = 10) -> list[dict]:
        ids = await self._index.recent_ids(limit)
        entries = []
        stale = []
        for entry_id in ids:
            raw = await self._redis.get(self._key(entry_id))
            if raw is None:
                stale.append(entry_id)
                continue
            entries.append(json.loads(raw))
        await self._index.prune(stale)
        return entries
