"""What we remember about this installation between restarts, and nothing else.

Four facts and one claim. The facts are the anonymous id, the admin-UI language
last seen, when the installation was first seen and the day its setup wizard
was finished; the claim is "these UTC days are already taken" — one key per
day, held by whichever replica got there first.

The claim lives here rather than in the sender because it is the same
keyspace and the same Redis handle, and `util/redis_keyspace.py` is easier to
read with one owner per prefix family than with two adapters over one hash.

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
from datetime import UTC, date, datetime
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from itop_ai_assistant.settings.module_locales import normalize_language
from itop_ai_assistant.util.redis_keyspace import (
    TELEMETRY_INSTALL_FIRST_SEEN_FIELD,
    TELEMETRY_INSTALL_ID_FIELD,
    TELEMETRY_INSTALL_KEY,
    TELEMETRY_INSTALL_LANGUAGE_FIELD,
    TELEMETRY_INSTALL_SETUP_DAY_FIELD,
    TELEMETRY_SENT_DAY_PREFIX,
    TELEMETRY_SENT_DAY_TTL_DAYS,
    days_to_seconds,
)

logger = logging.getLogger(__name__)


class InstallIdentity:
    """The installation's own id, the language it was last seen in, and the
    two dates the sender is not allowed to send before."""

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
        if await self._redis.hsetnx(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_ID_FIELD, candidate):
            return candidate
        # Ours was refused, so the winner's value is the one this installation
        # is known by. Nothing there means the key went away between the two
        # commands: the candidate is a truthful id for one installation, and
        # the alternative is the same non-answer from every installation in
        # that state at once.
        winner = await self._redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_ID_FIELD)
        return str(winner) if winner else candidate

    async def first_seen(self) -> datetime:
        """When this installation was first seen, recorded on first ask.

        Asked by the sender rather than written where the id is generated, so
        that an installation upgraded from a build that did not have the field
        gets an honest value on the first tick instead of no answer at all.
        Its own field and not the id's creation time: the id may predate this
        code, the field never does.
        """
        now = datetime.now(UTC)
        if await self._redis.hsetnx(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_FIRST_SEEN_FIELD, now.isoformat()):
            return now
        stored = await self._redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_FIRST_SEEN_FIELD)
        return datetime.fromisoformat(str(stored)) if stored else now

    async def note_setup_complete(self) -> None:
        """Record the day the setup wizard was finished. First one wins.

        `HSETNX`, so an installation reconfigured months later does not look
        like one that has just been set up — the first send happens once in an
        installation's life (REQ-009 R6).
        """
        today = datetime.now(UTC).date().isoformat()
        await self._redis.hsetnx(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_SETUP_DAY_FIELD, today)

    async def setup_day(self) -> date | None:
        """The day the wizard was finished, or `None` — including for every
        installation that finished it before this field existed."""
        stored = await self._redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_SETUP_DAY_FIELD)
        return date.fromisoformat(str(stored)) if stored else None

    async def claim_day(self, day: date) -> bool:
        """Take `day` for this replica, once per installation.

        False means somebody else already has it — the other replica of this
        installation, or this one an hour ago. The claim is taken *before* the
        send and is not released if the send fails: a lost day is allowed
        (R8), a day counted twice is not.
        """
        key = f"{TELEMETRY_SENT_DAY_PREFIX}{day.isoformat()}"
        return bool(await self._redis.set(key, "1", nx=True, ex=days_to_seconds(TELEMETRY_SENT_DAY_TTL_DAYS)))

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
