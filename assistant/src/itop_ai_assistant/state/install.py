"""What this installation remembers about itself between restarts.

Four facts and one claim. The facts are the anonymous id, the admin-UI language
last seen, when the installation was first seen and the day its setup wizard
was finished; the claim is "these UTC days are already taken" — one key per
day, held by whichever replica got there first.

Telemetry is the biggest reader here, not the owner. The id identifies the
*installation*: the admin UI shows it on the System screen, and a support
request names it. That is why this file
sits in `state/` rather than in `telemetry/`, and why its keys carry no
`telemetry:` prefix — the one exception being the claim, which says what
telemetry has already reported and is named for what it is.

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
    INSTALL_FIRST_SEEN_FIELD,
    INSTALL_ID_FIELD,
    INSTALL_KEY,
    INSTALL_LANGUAGE_FIELD,
    INSTALL_SETUP_DAY_FIELD,
    INSTALL_TELEMETRY_SENT_PREFIX,
    INSTALL_TELEMETRY_SENT_TTL_DAYS,
    days_to_seconds,
)

logger = logging.getLogger(__name__)


class InstallIdentity:
    """The installation's own id, the language it was last seen in, and the
    two dates the sender is not allowed to send before."""

    def __init__(self, redis: Redis):
        self._redis = redis

    async def register(self) -> None:
        """Put this installation on record: its id and when it was first seen.

        Called once at startup (`main.py`) so that the id exists before anyone
        asks for it — the System screen shows it, and a support request or a
        "delete my data" ask names it. Both writes are `HSETNX` underneath, so
        calling this on every start of every replica records nothing twice.

        Not the only way either value comes to exist: both getters still write
        on first ask, and that is what covers a start where Redis was down.
        """
        await self.install_id()
        await self.first_seen()

    async def install_id(self) -> str:
        """This installation's anonymous id, generated on first ask.

        `HSETNX` rather than `HSET`: two replicas starting at once must end up
        with the same id, so the one that loses the race reads the winner's
        value instead of keeping its own.
        """
        stored = await self._redis.hget(INSTALL_KEY, INSTALL_ID_FIELD)
        if stored:
            return str(stored)
        candidate = uuid4().hex
        if await self._redis.hsetnx(INSTALL_KEY, INSTALL_ID_FIELD, candidate):
            return candidate
        # Ours was refused, so the winner's value is the one this installation
        # is known by. Nothing there means the key went away between the two
        # commands: the candidate is a truthful id for one installation, and
        # the alternative is the same non-answer from every installation in
        # that state at once.
        winner = await self._redis.hget(INSTALL_KEY, INSTALL_ID_FIELD)
        return str(winner) if winner else candidate

    async def first_seen(self) -> datetime:
        """When this installation was first seen, recorded on first ask.

        Written at startup next to the id (`register`), so the moment means
        the first start rather than the first telemetry tick. Still recorded
        on first ask as well, which is what gives an honest value to an
        installation upgraded from a build without the field, or one whose
        Redis was down when it started.

        Its own field and not the id's creation time: the id may predate this
        code, the field never does.

        A value that cannot be read as a moment is replaced by this one rather
        than raised over. Both dates here are reachable by hand — a restore, a
        support session — and the sender guards only against `RedisError`, so
        a stray value would raise on every hourly tick and stop telemetry for
        the life of the installation, with nothing in the log but a tick that
        failed.
        """
        now = datetime.now(UTC)
        if await self._redis.hsetnx(INSTALL_KEY, INSTALL_FIRST_SEEN_FIELD, now.isoformat()):
            return now
        stored = await self._redis.hget(INSTALL_KEY, INSTALL_FIRST_SEEN_FIELD)
        moment = _as_moment(stored)
        if moment is not None:
            return moment
        logger.warning(f"Telemetry: {INSTALL_FIRST_SEEN_FIELD} is not a moment ({stored!r}), taken as now")
        await self._redis.hset(INSTALL_KEY, INSTALL_FIRST_SEEN_FIELD, now.isoformat())
        return now

    async def note_setup_complete(self) -> None:
        """Record the day the setup wizard was finished. First one wins.

        `HSETNX`, so an installation reconfigured months later does not look
        like one that has just been set up — the first send happens once in an
        installation's life (REQ-009 R6).
        """
        today = datetime.now(UTC).date().isoformat()
        await self._redis.hsetnx(INSTALL_KEY, INSTALL_SETUP_DAY_FIELD, today)

    async def setup_day(self) -> date | None:
        """The day the wizard was finished, or `None`.

        `None` covers three cases that the sender treats alike: no wizard was
        ever finished, it was finished before this field existed, and the
        field holds something that is not a date. The last one costs the first
        document; raising instead would cost every document after it.
        """
        stored = await self._redis.hget(INSTALL_KEY, INSTALL_SETUP_DAY_FIELD)
        if not stored:
            return None
        try:
            return date.fromisoformat(str(stored))
        except ValueError:
            logger.warning(f"Telemetry: {INSTALL_SETUP_DAY_FIELD} is not a date ({stored!r}), ignored")
            return None

    async def claim_day(self, day: date) -> bool:
        """Take `day` for this replica, once per installation.

        False means somebody else already has it — the other replica of this
        installation, or this one an hour ago. The claim is taken once the
        document exists and before it is sent, and it is not released if the
        send fails: a lost day is allowed (R8), a day counted twice is not.
        """
        key = f"{INSTALL_TELEMETRY_SENT_PREFIX}{day.isoformat()}"
        return bool(await self._redis.set(key, "1", nx=True, ex=days_to_seconds(INSTALL_TELEMETRY_SENT_TTL_DAYS)))

    async def language(self) -> str | None:
        """The last admin-UI language seen, or `None` if nobody has been in."""
        stored = await self._redis.hget(INSTALL_KEY, INSTALL_LANGUAGE_FIELD)
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
            await self._redis.hset(INSTALL_KEY, INSTALL_LANGUAGE_FIELD, normalized)
        except RedisError as e:
            logger.warning(f"Install state unavailable, language {normalized!r} not recorded: {e}")


def _as_moment(stored: object) -> datetime | None:
    """A stored timestamp, or `None` if it is not one.

    A value without a zone is read as UTC, which is the only zone this file
    ever writes. The alternative is not a stricter guard but a `TypeError`:
    the sender subtracts the result from an aware `now`.
    """
    if not stored:
        return None
    try:
        moment = datetime.fromisoformat(str(stored))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
