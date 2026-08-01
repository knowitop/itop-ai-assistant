"""The `schedule` entry point: the clock starts the run.

Third of three, and deliberately the same shape as the other two — resolve a
route, open the run frame, call the handler. What differs is only what the
other two differ in: who is waiting for the answer (nobody) and where the
subject comes from (the route, since a clock carries no payload).

A failure is logged and the loop keeps ticking — the scheduler
(`pipelines/scheduler.py`) already does that for every loop it owns, so the
tick lets the exception out and `journalled_run` records `failed` on the way.

One loop per **process**: two replicas tick twice. Whether that is acceptable
is the handler's business — the vector sweep takes a Postgres advisory lock,
a business job that must run once needs its own exclusion.
"""

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.models import RunOutcome
from itop_ai_assistant.pipelines.registry import ScheduleRoute, TriggerRegistry
from itop_ai_assistant.pipelines.runner import journalled_run
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks

logger = logging.getLogger(__name__)


def register_schedules(tasks: PeriodicTasks, registry: TriggerRegistry, deps: AppDeps) -> None:
    """Give every registered schedule route its own loop."""
    for route in registry.schedules:
        tasks.add(
            route.label,
            _tick(route, deps),
            interval=_interval(route, deps),
            default_interval=route.default_interval,
        )


async def run_schedule(route: ScheduleRoute, deps: AppDeps) -> RunOutcome:
    """One tick of one schedule route, journalled start to finish."""
    processing_id = uuid4()
    async with journalled_run(
        deps,
        processing_id,
        kind="schedule",
        subject=route.subject or route.label,
        event=route.name,
        module=route.module,
    ):
        outcome = await route.handler(processing_id, deps)
    logger.info(f"[{processing_id}] {route.label}: {outcome.status} {outcome.detail}".rstrip())
    return outcome


def _tick(route: ScheduleRoute, deps: AppDeps) -> Callable[[], Awaitable[RunOutcome]]:
    async def tick() -> RunOutcome:
        return await run_schedule(route, deps)

    return tick


def _interval(route: ScheduleRoute, deps: AppDeps) -> Callable[[], Awaitable[float]]:
    async def interval() -> float:
        return await route.interval_of(deps)

    return interval
