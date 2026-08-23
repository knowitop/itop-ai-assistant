"""What this installation did today, as fifteen integers — and nothing else.

The activity half of the anonymous telemetry document (REQ-009 R3). Deliberately
*not* computed from the run journal: journal entries expire after `run_ttl_days`
and their index is capped at `RUN_INDEX_MAX_ENTRIES`, so an installation with a
thousand tickets a day loses its own history from the index faster than the TTL
runs out. Counting a month of activity off that would be counting off an offcut
of unknown size.

One hash per UTC day rather than one key per counter: a single TTL for the day,
a single `HGETALL` to read it, and adding a counter touches neither the keyspace
nor the reader. The day is UTC like everything in the journal — the
installation's own offset travels as its own field of the document.

Writes are non-fatal, reads are not, for the same reason as `RunJournal`: a
counter able to break a run is worse than a missing counter (a telemetry
counter, doubly so). Nothing here knows about sending, and nothing here can
hold anything but an integer — which is the point R2 makes about the whole
shape of this requirement.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import StrEnum

from redis.asyncio import Redis
from redis.exceptions import RedisError

from itop_ai_assistant.state.journal import TriggerKind
from itop_ai_assistant.util.redis_keyspace import (
    TELEMETRY_COUNTERS_PREFIX,
    TELEMETRY_COUNTERS_TTL_DAYS,
    days_to_seconds,
)

logger = logging.getLogger(__name__)


class Counter(StrEnum):
    """Everything we count, in one list — which is also the list of everything
    the activity group of the document can contain.

    Names say what physically happened, not what it meant: a public comment is
    a public comment, and "the intake module asked a question" is a reading of
    it that belongs to the document builder, in one place, rather than to
    fifteen call sites. The three iTop counters are incremented where the
    writes physically pass (`TicketRepository`) precisely so that a module
    cannot forget to — including a module that has not been written yet.

    `runs_done` is absent on purpose: it is the trigger counters minus the
    failed and the skipped, and a fourth counter would be a fourth thing that
    can disagree with the other three.
    """

    RUNS_WEBHOOK = "runs_webhook"
    RUNS_REQUEST = "runs_request"
    RUNS_SCHEDULE = "runs_schedule"
    RUNS_FAILED = "runs_failed"
    # Lock held, object gone, or the module's guard said no. Without this one
    # "runs by status" reads as if every webhook did work — for a typical
    # installation most of them do not. Which of the three stages stopped the
    # run is not recorded: it would decide nothing, and the guard's reason is
    # free text a module wrote (R4).
    RUNS_SKIPPED = "runs_skipped"

    ITOP_PUBLIC_COMMENT = "itop_public_comment"
    ITOP_PRIVATE_NOTE = "itop_private_note"
    ITOP_FIELD_UPDATE = "itop_field_update"

    LLM_CALLS = "llm_calls"
    LLM_FAILURES = "llm_failures"
    LLM_TOKENS_IN = "llm_tokens_in"
    LLM_TOKENS_OUT = "llm_tokens_out"

    VECTOR_SEARCHES = "vector_searches"
    VECTOR_SEARCHES_EMPTY = "vector_searches_empty"
    VECTOR_CHUNKS_EMBEDDED = "vector_chunks_embedded"

    @staticmethod
    def for_trigger(kind: TriggerKind) -> "Counter | None":
        """The run counter of one trigger kind — here rather than in the run
        frame, so the frame holds no table of its own.

        `None` for a kind this table does not name, which the type system
        already forbids: a trigger added without its counter must cost the
        installation a number, not the run itself.
        """
        counter = _BY_TRIGGER.get(kind)
        if counter is None:
            logger.warning(f"No telemetry counter for trigger kind {kind!r}, run not counted")
        return counter


_BY_TRIGGER: dict[TriggerKind, Counter] = {
    "webhook": Counter.RUNS_WEBHOOK,
    "request": Counter.RUNS_REQUEST,
    "schedule": Counter.RUNS_SCHEDULE,
}


class DailyCounters:
    """The day's counters in Redis: increment what moved, read them all."""

    def __init__(self, redis: Redis, ttl_seconds: int = days_to_seconds(TELEMETRY_COUNTERS_TTL_DAYS)):
        self._redis = redis
        self._ttl = ttl_seconds

    def _key(self, day: date) -> str:
        return f"{TELEMETRY_COUNTERS_PREFIX}{day.isoformat()}"

    async def bump(self, counter: Counter, amount: int = 1) -> None:
        """Add to today's counter. Never raises — the caller is on a hot path."""
        await self.bump_many({counter: amount})

    async def bump_many(self, amounts: Mapping[Counter, int]) -> None:
        """The same, for counters that always move together — one round trip
        instead of one per counter, which is what keeps the model handler off
        the agent loop's critical path (`core/llm_counters.py`).

        A non-positive amount writes nothing at all: a model that reported no
        token usage must not be the reason a day's key exists.
        """
        wanted = {counter: amount for counter, amount in amounts.items() if amount > 0}
        if not wanted:
            return
        key = self._key(datetime.now(UTC).date())
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for counter, amount in wanted.items():
                    pipe.hincrby(key, counter.value, amount)
                pipe.expire(key, self._ttl)
                await pipe.execute()
        except RedisError as e:
            names = ", ".join(counter.value for counter in wanted)
            logger.warning(f"Telemetry counters unavailable, {names} not recorded: {e}")

    async def read(self, day: date) -> dict[Counter, int]:
        """One day, every counter, zeros included — the document's shape must
        not depend on what happened to be incremented."""
        raw = await self._redis.hgetall(key := self._key(day))
        counters = {c: 0 for c in Counter}
        for name, value in raw.items():
            try:
                counter = Counter(name)
            except ValueError:
                # A field left by an older version, or a counter since renamed:
                # reported, then ignored. The document carries what this
                # version knows how to name.
                logger.warning(f"Unknown telemetry counter {name!r} in {key}, ignored")
                continue
            try:
                counters[counter] = int(value)
            except ValueError:
                # Something wrote a non-integer under a name we do know. Said
                # apart from the case above: the two send a reader looking in
                # very different places.
                logger.warning(f"Telemetry counter {name!r} in {key} holds {value!r}, not a number; read as 0")
        return counters
