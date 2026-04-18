import logging
from dataclasses import dataclass

from redis import RedisError
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
_KEY_PREFIX = "ticket:"


class StateUnavailableError(Exception):
    pass


@dataclass
class TicketState:
    rounds: int = 0
    classify_rounds: int = 0
    ai_done: bool = False


class TicketStateManager:
    def __init__(self, redis: Redis):
        """
        :param redis: redis.asyncio.Redis client configured with decode_responses=True.
        """
        self._redis = redis

    def _key(self, ticket_ref: str) -> str:
        return f"{_KEY_PREFIX}{ticket_ref}"

    async def get(self, ticket_ref: str) -> TicketState:
        """Return current state for a ticket. Defaults to rounds=0, ai_done=False if not found."""
        try:
            data: dict = await self._redis.hgetall(self._key(ticket_ref))  # type: ignore[misc]
        except RedisError as e:
            logger.error(f"Redis error getting state for ticket {ticket_ref}: {e}")
            raise StateUnavailableError(f"Redis unavailable: {e}") from e

        if not data:
            return TicketState()

        return TicketState(
            rounds=int(data.get("rounds", 0)),
            classify_rounds=int(data.get("classify_rounds", 0)),
            ai_done=data.get("ai_done", "0") == "1",
        )

    async def increment_rounds(self, ticket_ref: str) -> None:
        """Atomically increment rounds counter and reset TTL to 30 days."""
        key = self._key(ticket_ref)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hincrby(key, "rounds", 1)
                pipe.expire(key, TTL_SECONDS)
                await pipe.execute()
        except RedisError as e:
            logger.error(f"Redis error incrementing rounds for ticket {ticket_ref}: {e}")
            raise StateUnavailableError(f"Redis unavailable: {e}") from e

    async def increment_classify_rounds(self, ticket_ref: str) -> None:
        """Atomically increment classify_rounds counter and reset TTL to 30 days."""
        key = self._key(ticket_ref)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hincrby(key, "classify_rounds", 1)
                pipe.expire(key, TTL_SECONDS)
                await pipe.execute()
        except RedisError as e:
            logger.error(f"Redis error incrementing classify_rounds for ticket {ticket_ref}: {e}")
            raise StateUnavailableError(f"Redis unavailable: {e}") from e

    async def mark_done(self, ticket_ref: str) -> None:
        """Mark AI processing as done and reset TTL to 30 days."""
        key = self._key(ticket_ref)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, "ai_done", "1")
                pipe.expire(key, TTL_SECONDS)
                await pipe.execute()
        except RedisError as e:
            logger.error(f"Redis error marking done for ticket {ticket_ref}: {e}")
            raise StateUnavailableError(f"Redis unavailable: {e}") from e


def create_state_manager() -> TicketStateManager:
    import redis.asyncio as aioredis

    from config import get_settings

    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return TicketStateManager(client)


state_manager = create_state_manager()
