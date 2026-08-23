"""What we remember about this installation between restarts: two fields.

The anonymous id (REQ-009 R1) is generated here, once, from nothing — not from
the iTop URL, not from an organization name, not from a key. Deriving it from
anything the installation already holds would make it a fingerprint of that
thing, and the whole requirement rests on it being neither.

It lives in Redis rather than a file in a volume. Redis is the only state this
service owns, and a second home for state would buy a property that is not
even whole: a volume is deleted together with Redis more often than apart from
it. The cost is real and is written down rather than worked around — a Redis
reset makes this installation look like a new one and inflates the count of
installations. `docs/telemetry.md` says so.

The language (R10) is not asked for anywhere: the admin SPA already sends
`?lang=` with the requests it makes, and the last value seen is remembered.
An installation nobody has opened has no language, and reports none rather
than a default somebody invented.

Reads do not swallow `RedisError`. Without Redis there is nothing to build a
document from — the counters live there too — and an installation that cannot
be identified must not be sent as one. R5 goes further and forbids making the
sender resilient to Redis being gone at all: a cached last document or an
in-memory fallback would return that failure silently, and for free to
whoever added it.
"""

import logging
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from itop_ai_assistant.settings.module_locales import normalize_language
from itop_ai_assistant.util.redis_keyspace import (
    TELEMETRY_INSTALL_ID_FIELD,
    TELEMETRY_INSTALL_KEY,
    TELEMETRY_INSTALL_LANGUAGE_FIELD,
)

logger = logging.getLogger(__name__)


class InstallIdentity:
    """The installation's own id, and the language it was last seen in."""

    def __init__(self, redis: Redis):
        self._redis = redis

    async def install_id(self) -> str:
        """This installation's anonymous id, generated on first ask.

        `HSETNX` rather than `HSET`: two replicas starting at once must end up
        with the same id, so the one that loses the race reads the winner's
        value instead of keeping its own.
        """
        stored = await self._redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_ID_FIELD)
        if stored:
            return str(stored)
        candidate = uuid4().hex
        await self._redis.hsetnx(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_ID_FIELD, candidate)
        # Re-read rather than trusting the return value: the loser of the race
        # must hand back what the winner wrote.
        return str(await self._redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_ID_FIELD))

    async def language(self) -> str | None:
        """The last admin-UI language seen, or `None` if nobody has been in."""
        stored = await self._redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_LANGUAGE_FIELD)
        return str(stored) if stored else None

    async def remember_language(self, lang: str | None) -> None:
        """Record the language of an admin request. Never raises.

        Anything that is not a language tag is not recorded at all — the value
        arrives from a query string, and `normalize_language` is the same guard
        the translation loader already puts in front of a file name.
        """
        normalized = normalize_language(lang)
        if normalized is None:
            return
        try:
            await self._redis.hset(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_LANGUAGE_FIELD, normalized)
        except RedisError as e:
            logger.warning(f"Telemetry install state unavailable, language {normalized!r} not recorded: {e}")
