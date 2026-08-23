"""The outer frame of a run: opened once, closed whatever happens inside.

Both entry points share it, so it is pinned here rather than through either one.
"""

import unittest
from datetime import UTC, datetime
from uuid import uuid4

import fakeredis.aioredis

from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.core.tracing import NullRunTracer
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import RunOutcome
from itop_ai_assistant.pipelines.runner import journalled_run
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.state.journal import RunJournal

_TOKEN = "s3cret-engineer-token"
_ENGINEER = Principal.delegated(_TOKEN, login="jdoe", name="John Doe")


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The frame needs a journal, a tracer and the counters, and nothing
        # else — no container to assemble. Tracing off is the default state, so
        # that is what the frame is exercised with here; `test_tracing.py` owns
        # the tracer.
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.journal = RunJournal(redis)
        self.counters = DailyCounters(redis)
        self.tracer = NullRunTracer()
        self.pid = uuid4()

    def _run(self, kind="webhook", principal=None):
        run = RunContext(processing_id=self.pid, module="intake", principal=principal or Principal.service())
        return journalled_run(
            self.journal, self.tracer, self.counters, run, kind=kind, subject="UserRequest::42", event="created"
        )


class TestCounted(RunnerTestCase):
    """The platform's own counting, all of it in one place: no module knows
    that runs are counted, so none can forget to (REQ-009 R3)."""

    async def _counters(self) -> dict:
        return await self.counters.read(datetime.now(UTC).date())

    async def test_a_run_is_counted_under_the_trigger_that_opened_it(self):
        async with self._run(kind="schedule"):
            pass

        counters = await self._counters()

        self.assertEqual(1, counters[Counter.RUNS_SCHEDULE])
        self.assertEqual(0, counters[Counter.RUNS_WEBHOOK])

    async def test_a_failed_run_is_counted_twice_by_kind_and_as_a_failure(self):
        with self.assertRaises(RuntimeError):
            async with self._run():
                raise RuntimeError("iTop is down")

        counters = await self._counters()

        self.assertEqual(1, counters[Counter.RUNS_WEBHOOK])
        self.assertEqual(1, counters[Counter.RUNS_FAILED])

    async def test_a_skip_is_counted_from_the_outcome_the_body_reports(self):
        """The number that separates "a thousand tickets processed" from "a
        thousand webhooks arrived and the guard sent them all home". Counted
        here rather than in `TicketRun.skip`, so a module that reports a skip
        its own way — selfcheck's guard does — reaches the same counter."""
        async with self._run() as frame:
            frame.result = RunOutcome(status="skipped", detail="not our business")

        counters = await self._counters()

        self.assertEqual(1, counters[Counter.RUNS_WEBHOOK])
        self.assertEqual(1, counters[Counter.RUNS_SKIPPED])

    async def test_a_run_that_did_its_work_is_not_a_skip(self):
        async with self._run() as frame:
            frame.result = RunOutcome(status="done", detail="asked a question")

        self.assertEqual(0, (await self._counters())[Counter.RUNS_SKIPPED])

    async def test_a_body_that_reported_nothing_reported_no_skip(self):
        """`WebhookHandler` may return anything at all, so the frame counts a
        skip only where it was actually told about one."""
        async with self._run() as frame:
            frame.result = "whatever a handler felt like returning"

        self.assertEqual(0, (await self._counters())[Counter.RUNS_SKIPPED])


class TestJournalledRun(RunnerTestCase):
    async def test_clean_run_is_recorded_start_to_finish(self):
        async with self._run():
            pass

        run = await self.journal.get(self.pid)
        self.assertEqual(run.status, "done")
        self.assertEqual(run.subject, "UserRequest::42")
        self.assertEqual(run.module, "intake")
        self.assertIsNotNone(run.finished_at)

    async def test_kind_travels_to_the_journal(self):
        async with self._run(kind="request"):
            pass

        self.assertEqual((await self.journal.get(self.pid)).kind, "request")

    async def test_the_principal_travels_to_the_journal(self):
        async with self._run(principal=_ENGINEER):
            pass

        self.assertEqual((await self.journal.get(self.pid)).principal, "engineer:jdoe")

    async def test_the_token_reaches_no_part_of_the_record(self):
        """Reading under an engineer's token leaves almost no trace in iTop, so
        the journal is where "who asked" lives — and must not become where their
        credential lives."""
        async with self._run(principal=_ENGINEER):
            pass

        stored = await self.journal._redis.hgetall(f"run:{self.pid}")
        self.assertNotIn(_TOKEN, "".join([*stored.keys(), *stored.values()]))

    async def test_failure_is_recorded_and_re_raised(self):
        """Swallowing is the entry point's decision, not the frame's."""
        with self.assertRaises(RuntimeError):
            async with self._run():
                raise RuntimeError("boom")

        run = await self.journal.get(self.pid)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "RuntimeError: boom")


if __name__ == "__main__":
    unittest.main()
