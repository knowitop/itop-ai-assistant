"""The third entry point: the clock opens a run like any other trigger.

Pinned through a probe route on a throwaway registry, not through a module —
the entry point must know a registry entry, not a module by name.
"""

import asyncio
import unittest
from unittest.mock import MagicMock

import fakeredis.aioredis

from itop_ai_assistant.journal import RunJournal
from itop_ai_assistant.pipelines.models import RunOutcome
from itop_ai_assistant.pipelines.registry import ModuleInfo, ScheduleRoute, TriggerRegistry
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.schedule.runner import register_schedules, run_schedule


class ScheduleRunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = MagicMock()
        self.deps.journal = RunJournal(fakeredis.aioredis.FakeRedis(decode_responses=True))
        self.calls: list = []

    def _route(self, handler=None, **overrides) -> ScheduleRoute:
        async def default_handler(processing_id, deps) -> RunOutcome:
            self.calls.append(processing_id)
            return RunOutcome(status="done", detail="probed")

        return ScheduleRoute(
            name="tick",
            module="probe",
            handler=handler or default_handler,
            interval_of=overrides.pop("interval_of", None) or self._interval(60.0),
            **overrides,
        )

    @staticmethod
    def _interval(seconds: float):
        async def interval_of(deps) -> float:
            return seconds

        return interval_of

    async def _only_run(self):
        runs = await self.deps.journal.list()
        self.assertEqual(len(runs), 1)
        return await self.deps.journal.get(runs[0].processing_id)


class TestRunSchedule(ScheduleRunnerTestCase):
    async def test_tick_is_journalled_as_a_schedule_run(self):
        outcome = await run_schedule(self._route(), self.deps)

        self.assertEqual(outcome.status, "done")
        run = await self._only_run()
        self.assertEqual(run.kind, "schedule")
        self.assertEqual(run.module, "probe")
        self.assertEqual(run.event, "tick")
        self.assertEqual(run.status, "done")

    async def test_subject_defaults_to_the_route_label(self):
        """A clock carries no payload, so the subject is the route itself —
        and it is not a ticket."""
        await run_schedule(self._route(), self.deps)

        self.assertEqual((await self._only_run()).subject, "probe/tick")

    async def test_explicit_subject_wins(self):
        await run_schedule(self._route(subject="service catalog"), self.deps)

        self.assertEqual((await self._only_run()).subject, "service catalog")

    async def test_handler_gets_the_run_id(self):
        await run_schedule(self._route(), self.deps)

        run = await self._only_run()
        self.assertEqual(str(self.calls[0]), run.processing_id)

    async def test_failure_is_journalled_and_propagates(self):
        """The loop swallows it — the frame must not, or the journal would
        record a clean run over a broken tick."""

        async def boom(processing_id, deps) -> RunOutcome:
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await run_schedule(self._route(handler=boom), self.deps)

        run = await self._only_run()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "RuntimeError: boom")


class TestRegisterSchedules(ScheduleRunnerTestCase):
    def _registry(self, *routes) -> TriggerRegistry:
        registry = TriggerRegistry()
        registry.register(ModuleInfo(name="probe", description="Probe"), schedules=list(routes))
        return registry

    async def test_one_loop_per_route(self):
        tasks = PeriodicTasks()
        route = self._route()
        register_schedules(tasks, self._registry(route), self.deps)

        self.assertEqual(tasks.names, ["probe/tick"])

    async def test_a_module_without_schedules_gets_no_loop(self):
        tasks = PeriodicTasks()
        register_schedules(tasks, self._registry(), self.deps)

        self.assertEqual(tasks.names, [])

    async def test_interval_is_taken_from_the_route(self):
        tasks = PeriodicTasks()
        register_schedules(tasks, self._registry(self._route(interval_of=self._interval(7.0))), self.deps)

        self.assertEqual(await tasks._entries["probe/tick"].interval(), 7.0)

    async def test_started_loop_runs_the_route(self):
        tasks = PeriodicTasks()
        register_schedules(tasks, self._registry(self._route(interval_of=self._interval(3600))), self.deps)
        tasks.start()
        try:
            for _ in range(200):
                if self.calls:
                    break
                await asyncio.sleep(0.005)
        finally:
            await tasks.stop()

        self.assertEqual(len(self.calls), 1)
        self.assertEqual((await self._only_run()).kind, "schedule")

    async def test_a_failing_tick_does_not_stop_the_loop(self):
        seen = asyncio.Event()

        async def boom(processing_id, deps) -> RunOutcome:
            self.calls.append(processing_id)
            seen.set()
            raise RuntimeError("boom")

        tasks = PeriodicTasks()
        route = self._route(handler=boom, interval_of=self._interval(0.01))
        register_schedules(tasks, self._registry(route), self.deps)
        tasks.start()
        try:
            await asyncio.wait_for(seen.wait(), 2.0)
            await asyncio.sleep(0.05)
            self.assertTrue(tasks.is_running("probe/tick"))
        finally:
            await tasks.stop()

        self.assertGreater(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
