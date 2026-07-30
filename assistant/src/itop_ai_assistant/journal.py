"""Processing-run journal: per-run status and step trace in Redis.

Observability layer consumed by the admin API (and the future UI). Write
methods are non-fatal by design — journal failures must never break ticket
processing. Read methods raise so API clients see a real error.
"""

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_RUN_PREFIX = "run:"
_INDEX_KEY = "runs:index"
_MAX_INDEXED_RUNS = 1000
_LIST_SCAN_WINDOW = 500

RunStatus = Literal["running", "done", "failed"]


class RunStep(BaseModel):
    at: datetime
    node: str
    detail: str = ""


class ProcessingRun(BaseModel):
    processing_id: str
    ticket: str
    event: str
    module: str
    status: RunStatus = "running"
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    steps: list[RunStep] = []


class RunJournal:
    def __init__(self, redis: Redis, ttl_seconds: int = 7 * 24 * 60 * 60):
        self._redis = redis
        self._ttl = ttl_seconds

    def _key(self, processing_id: UUID | str) -> str:
        return f"{_RUN_PREFIX}{processing_id}"

    def _steps_key(self, processing_id: UUID | str) -> str:
        return f"{_RUN_PREFIX}{processing_id}:steps"

    async def start(self, processing_id: UUID | str, ticket: str, event: str, module: str) -> None:
        now = datetime.now(UTC)
        key = self._key(processing_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hset(
                    key,
                    mapping={
                        "processing_id": str(processing_id),
                        "ticket": ticket,
                        "event": event,
                        "module": module,
                        "status": "running",
                        "started_at": now.isoformat(),
                    },
                )
                pipe.expire(key, self._ttl)
                pipe.zadd(_INDEX_KEY, {str(processing_id): now.timestamp()})
                pipe.zremrangebyrank(_INDEX_KEY, 0, -_MAX_INDEXED_RUNS - 1)
                await pipe.execute()
        except RedisError as e:
            logger.warning(f"Run journal unavailable, start not recorded for {processing_id}: {e}")

    async def add_step(self, processing_id: UUID | str, node: str, detail: str = "") -> None:
        step = RunStep(at=datetime.now(UTC), node=node, detail=detail)
        key = self._steps_key(processing_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.rpush(key, step.model_dump_json())
                pipe.expire(key, self._ttl)
                await pipe.execute()
        except RedisError as e:
            logger.warning(f"Run journal unavailable, step not recorded for {processing_id}: {e}")

    async def finish(self, processing_id: UUID | str, status: RunStatus, error: str | None = None) -> None:
        fields: dict = {"status": status, "finished_at": datetime.now(UTC).isoformat()}
        if error:
            fields["error"] = error
        try:
            await self._redis.hset(self._key(processing_id), mapping=fields)
        except RedisError as e:
            logger.warning(f"Run journal unavailable, finish not recorded for {processing_id}: {e}")

    async def get(self, processing_id: UUID | str) -> ProcessingRun | None:
        data = await self._redis.hgetall(self._key(processing_id))
        if not data:
            return None
        raw_steps = await self._redis.lrange(self._steps_key(processing_id), 0, -1)
        steps = [RunStep.model_validate_json(raw) for raw in raw_steps]
        return ProcessingRun(**data, steps=steps)

    async def list(self, limit: int = 50, ticket: str | None = None, status: str | None = None) -> list[ProcessingRun]:
        """Most recent runs first. Steps are not loaded — use get() for details."""
        ids = await self._redis.zrevrange(_INDEX_KEY, 0, _LIST_SCAN_WINDOW - 1)
        runs: list[ProcessingRun] = []
        stale: list[str] = []
        for processing_id in ids:
            data = await self._redis.hgetall(self._key(processing_id))
            if not data:
                stale.append(processing_id)
                continue
            run = ProcessingRun(**data)
            if ticket and run.ticket != ticket:
                continue
            if status and run.status != status:
                continue
            runs.append(run)
            if len(runs) >= limit:
                break
        if stale:
            await self._redis.zrem(_INDEX_KEY, *stale)
        return runs
