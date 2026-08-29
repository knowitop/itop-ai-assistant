"""The pacing primitive: it fires, it re-reads, it survives a bad tick.

Intervals here are microscopic and every wait is on an Event, so nothing in
this file sleeps for real time.
"""

import asyncio
import unittest

from itop_ai_assistant.pipelines.scheduler import PeriodicTasks


class Ticker:
    """Counts ticks and lets a test await the Nth one."""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail
        self._reached = asyncio.Event()
        self._target = 1

    async def tick(self) -> None:
        self.calls += 1
        if self.calls >= self._target:
            self._reached.set()
        if self.fail:
            raise RuntimeError("boom")

    async def wait_for(self, count: int, timeout: float = 2.0) -> None:
        self._target = count
        if self.calls < count:
            self._reached.clear()
            await asyncio.wait_for(self._reached.wait(), timeout)


class SchedulerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tasks = PeriodicTasks()

    async def asyncTearDown(self):
        await self.tasks.stop()

    @staticmethod
    def _interval(seconds: float = 0.01):
        async def interval() -> float:
            return seconds

        return interval


class TestLoop(SchedulerTestCase):
    async def test_first_tick_fires_immediately(self):
        """A boot must not wait out a full interval before doing anything."""
        ticker = Ticker()
        self.tasks.add("probe", ticker.tick, self._interval(3600), default_interval=3600)
        self.tasks.start()

        await ticker.wait_for(1)
        self.assertEqual(ticker.calls, 1)

    async def test_interval_is_re_read_before_every_wait(self):
        seen = []

        async def interval() -> float:
            seen.append(len(seen))
            return 0.01

        ticker = Ticker()
        self.tasks.add("probe", ticker.tick, interval, default_interval=0.01)
        self.tasks.start()

        await ticker.wait_for(3)
        # One lookup per completed tick — a config edit applies from the next wait
        self.assertGreaterEqual(len(seen), 2)

    async def test_failing_tick_does_not_kill_the_loop(self):
        ticker = Ticker(fail=True)
        self.tasks.add("probe", ticker.tick, self._interval(), default_interval=0.01)
        self.tasks.start()

        await ticker.wait_for(3)
        self.assertTrue(self.tasks.is_running("probe"))

    async def test_broken_interval_falls_back_to_the_default(self):
        """Redis down must not turn a 5-minute sweep into a hot loop."""

        async def interval() -> float:
            raise RuntimeError("config store is down")

        ticker = Ticker()
        self.tasks.add("probe", ticker.tick, interval, default_interval=0.01)
        self.tasks.start()

        await ticker.wait_for(2)
        self.assertGreaterEqual(ticker.calls, 2)

    async def test_non_positive_interval_falls_back_to_the_default(self):
        ticker = Ticker()
        self.tasks.add("probe", ticker.tick, self._interval(0), default_interval=0.01)
        self.tasks.start()

        await ticker.wait_for(2)
        self.assertGreaterEqual(ticker.calls, 2)


class TestControl(SchedulerTestCase):
    async def test_wake_interrupts_a_long_wait(self):
        ticker = Ticker()
        self.tasks.add("probe", ticker.tick, self._interval(3600), default_interval=3600)
        self.tasks.start()
        await ticker.wait_for(1)

        self.assertTrue(self.tasks.wake("probe"))
        await ticker.wait_for(2)
        self.assertEqual(ticker.calls, 2)

    async def test_a_tick_can_tell_a_wake_from_the_timer(self):
        """What the vector sweep reads to let "Index now" through a pacing
        gate a timer tick is subject to."""
        ticker = Ticker()
        seen: list[bool] = []

        async def tick() -> None:
            seen.append(self.tasks.was_woken("probe"))
            await ticker.tick()

        self.tasks.add("probe", tick, self._interval(3600), default_interval=3600)
        self.tasks.start()
        await ticker.wait_for(1)

        self.tasks.wake("probe")
        await ticker.wait_for(2)

        self.assertEqual(seen, [False, True])

    async def test_a_wake_delivered_to_a_stopped_loop_does_not_survive_the_restart(self):
        """Otherwise the first tick after a start reads as requested and the
        vector sweep runs unpaced — exactly what the pacing gate removes."""
        ticker = Ticker()
        seen: list[bool] = []

        async def tick() -> None:
            seen.append(self.tasks.was_woken("probe"))
            await ticker.tick()

        self.tasks.add("probe", tick, self._interval(3600), default_interval=3600)
        self.tasks.start()
        await ticker.wait_for(1)
        await self.tasks.stop()

        self.tasks.wake("probe")
        self.tasks.start()
        await ticker.wait_for(2)

        self.assertEqual(seen, [False, False])

    async def test_wake_reports_an_unknown_task(self):
        self.assertFalse(self.tasks.wake("nope"))

    async def test_stop_cancels_the_loop(self):
        ticker = Ticker()
        self.tasks.add("probe", ticker.tick, self._interval(), default_interval=0.01)
        self.tasks.start()
        await ticker.wait_for(1)

        await self.tasks.stop()
        self.assertFalse(self.tasks.is_running("probe"))
        after = ticker.calls
        await asyncio.sleep(0.05)
        self.assertEqual(ticker.calls, after)

    async def test_stop_without_start_is_a_no_op(self):
        self.tasks.add("probe", Ticker().tick, self._interval(), default_interval=0.01)
        await self.tasks.stop()
        self.assertFalse(self.tasks.is_running("probe"))

    async def test_is_running_is_false_for_an_unknown_task(self):
        self.assertFalse(self.tasks.is_running("nope"))

    async def test_duplicate_name_is_rejected(self):
        self.tasks.add("probe", Ticker().tick, self._interval(), default_interval=0.01)
        with self.assertRaises(ValueError):
            self.tasks.add("probe", Ticker().tick, self._interval(), default_interval=0.01)

    async def test_non_positive_default_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            self.tasks.add("probe", Ticker().tick, self._interval(), default_interval=0)


if __name__ == "__main__":
    unittest.main()
