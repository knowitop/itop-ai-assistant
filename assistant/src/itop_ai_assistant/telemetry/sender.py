"""Whether to send, and for which day. Everything else belongs to somebody else.

Infrastructure, not a business module — the same shelf as the vector sweep and
for the same reason (`pipelines/registry.py`): a tick with no module, no
subject and no principal is not a trigger, and giving it a schedule route
would mean inventing all three.

The tick ticks hourly, and the hour is not the period of anything. What paces
sending is the UTC day: one document per installation per day, and a day taken
in Redis before the send rather than after it (`install.claim_day`). Hourly
means an installation that was down at midnight still reports the day it was
up for, and it means the phase of the cycle is the moment each installation
happened to restart — so nothing synchronises the fleet on the day boundary.

Which day, in full:

* the day the setup wizard was finished — once in an installation's life, and
  only for an installation that finished it while this code was running. That
  document covers a partial day, and it is the only one that ever does;
* otherwise yesterday, whole, and not before a day has passed since the
  installation was first seen (REQ-009 R6). The floor matters on *upgrades*:
  "the wizard is finished" is true a second after a restart, and without it an
  installation would report before anyone could have found the switch.

Failure is a non-event in both directions (R8). A receiver that times out or
answers 500 leaves nothing behind — no journal entry, no alarm, no queue, and
the day stays claimed, so nothing retries it tomorrow. Redis being gone ends
the tick just as quietly: there is nothing to build a document from, and R5
forbids giving this loop a way around that.
"""

import logging
from datetime import UTC, date, datetime, timedelta

from redis.exceptions import RedisError

from itop_ai_assistant.config import ItopConfig, LlmConfig, TelemetryConfig, missing_setup
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.telemetry.builder import DocumentBuilder
from itop_ai_assistant.telemetry.install import InstallIdentity
from itop_ai_assistant.telemetry.ports import TelemetrySink

logger = logging.getLogger(__name__)

#: The scheduler's name for the loop — `admin/setup.py` wakes it by this name
#: when the wizard is finished, instead of leaving the first document to wait
#: out the hour.
SEND_TASK = "telemetry-send"

#: How often the tick asks its questions. Not a setting: the section has one
#: field on purpose (REQ-009 R5), and a period that is not the period of
#: anything would be a knob with nothing behind it.
_TICK_INTERVAL_SECONDS = 3600.0


def register_telemetry_send(
    tasks: PeriodicTasks,
    config_store: ConfigStore,
    builder: DocumentBuilder,
    install: InstallIdentity,
    sink: TelemetrySink,
) -> None:
    """Put the daily send under the process-wide scheduler.

    Registered whether or not telemetry is on: the switch is runtime state
    (REQ-009 R5), so "off" has to be a question the tick asks, not a loop that
    was never started. Asking it costs one config read an hour and nothing
    else — no client is built and no name is resolved until the answer is yes.
    """

    async def interval() -> float:
        return _TICK_INTERVAL_SECONDS

    tasks.add(
        SEND_TASK,
        TelemetrySender(config_store, builder, install, sink).tick,
        interval=interval,
        default_interval=_TICK_INTERVAL_SECONDS,
    )


class TelemetrySender:
    """One tick: decide, claim, build, hand over."""

    def __init__(
        self,
        config_store: ConfigStore,
        builder: DocumentBuilder,
        install: InstallIdentity,
        sink: TelemetrySink,
    ) -> None:
        self._config = config_store
        self._builder = builder
        self._install = install
        self._sink = sink

    async def tick(self) -> None:
        try:
            await self._send_if_due()
        except RedisError as e:
            # Not an error of ours to report: without Redis there are no
            # counters and no install id, so there is no document — and no
            # claim was taken, so nothing is lost that was not lost anyway.
            logger.info(f"telemetry: nothing to send, installation state unavailable: {e}")

    async def _send_if_due(self) -> None:
        if not (await self._config.get("telemetry", TelemetryConfig)).enabled:
            return
        itop = await self._config.get("itop", ItopConfig)
        llm = await self._config.get("llm", LlmConfig)
        if missing_setup(itop, llm):
            return

        due = await self._day_due()
        if due is None:
            return
        day, first = due
        if not await self._install.claim_day(day):
            return

        document = await self._builder.build(day)
        if await self._sink.send(document, first=first):
            logger.info(f"telemetry: document for {day.isoformat()} sent")

    async def _day_due(self) -> tuple[date, bool] | None:
        """The day to send, and whether it is this installation's first document."""
        now = datetime.now(UTC)
        today = now.date()
        if await self._install.setup_day() == today:
            return today, True
        if now - await self._install.first_seen() >= timedelta(days=1):
            return today - timedelta(days=1), False
        return None
