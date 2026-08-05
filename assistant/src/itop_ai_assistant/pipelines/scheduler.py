"""Periodic background loops: pacing, and nothing else.

Every loop in the process lives here — one asyncio task per entry, started
and stopped together with the app. What a tick *means* is deliberately not
this module's business, and that is the whole point:

* an **infrastructure** loop (the vector sweep) ticks bare — it has its own
  journal, its own cross-replica lock and no business identity at all;
* a **`schedule` trigger** ticks inside `journalled_run` — the frame belongs
  to the entry point (`schedule/runner.py`), exactly as it does for a webhook
  or a synchronous request.

Semantics a tick can rely on: it fires immediately on start, the interval is
re-read from config *before every wait* (so an edit in the admin UI applies
without a restart), a failing tick is logged and the loop lives on, and the
wait is interruptible — `wake(name)` runs the next tick now.

A loop is **per process**. Cross-replica exclusion is the tick's own business
(the vector sweep takes a Postgres advisory lock); there is no leader election
here.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: One iteration of the loop. Its return value is ignored — a tick reports
#: through its own journal, not to the scheduler.
Tick = Callable[[], Awaitable[object]]
#: Seconds until the next tick, resolved fresh before every wait.
Interval = Callable[[], Awaitable[float]]


@dataclass
class _Entry:
    name: str
    tick: Tick
    interval: Interval
    default_interval: float
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class PeriodicTasks:
    """Owns every periodic loop in the process."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def add(self, name: str, tick: Tick, interval: Interval, default_interval: float) -> None:
        """Register a loop. `default_interval` is used when `interval()` fails —
        a Redis outage must not turn a 5-minute sweep into a hot loop."""
        if name in self._entries:
            raise ValueError(f"Periodic task {name!r} is already registered")
        if default_interval <= 0:
            raise ValueError(f"Periodic task {name!r}: default_interval must be positive")
        self._entries[name] = _Entry(name=name, tick=tick, interval=interval, default_interval=default_interval)

    @property
    def names(self) -> list[str]:
        return list(self._entries)

    def start(self) -> None:
        for entry in self._entries.values():
            if entry.task is None or entry.task.done():
                entry.task = asyncio.create_task(self._run(entry), name=f"periodic:{entry.name}")
        logger.info(f"Started {len(self._entries)} periodic tasks: {', '.join(self._entries) or '—'}")

    async def stop(self) -> None:
        for entry in self._entries.values():
            if entry.task is not None:
                entry.task.cancel()
        for entry in self._entries.values():
            if entry.task is not None:
                with suppress(asyncio.CancelledError):
                    await entry.task
                entry.task = None

    def wake(self, name: str) -> bool:
        """Run the next tick now instead of at the end of the wait.

        Synchronous on purpose: an HTTP handler asks for it without awaiting.
        Returns False if no such task is registered.
        """
        entry = self._entries.get(name)
        if entry is None:
            return False
        entry.wake.set()
        return True

    def is_running(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry is not None and entry.task is not None and not entry.task.done()

    async def _run(self, entry: _Entry) -> None:
        while True:
            entry.wake.clear()
            try:
                await entry.tick()
            except Exception:
                # A loop that dies on one bad tick is worse than a noisy log:
                # nothing restarts it until the next deployment.
                logger.exception(f"periodic task {entry.name!r}: tick failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(entry.wake.wait(), timeout=await self._delay(entry))

    async def _delay(self, entry: _Entry) -> float:
        try:
            delay = await entry.interval()
        except Exception as e:
            logger.warning(f"periodic task {entry.name!r}: interval lookup failed ({e}), using the default")
            return entry.default_interval
        return delay if delay > 0 else entry.default_interval
