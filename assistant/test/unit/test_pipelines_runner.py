"""The outer frame of a run: opened once, closed whatever happens inside.

Both entry points share it, so it is pinned here rather than through either one.
"""

import unittest
from unittest.mock import MagicMock
from uuid import uuid4

import fakeredis.aioredis

from itop_ai_assistant.journal import RunJournal
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.runner import journalled_run
from itop_ai_assistant.principal import Principal

_TOKEN = "s3cret-engineer-token"
_ENGINEER = Principal.delegated(_TOKEN, login="jdoe", name="John Doe")


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = MagicMock()
        self.deps.journal = RunJournal(fakeredis.aioredis.FakeRedis(decode_responses=True))
        self.pid = uuid4()

    def _run(self, kind="webhook", principal=None):
        run = RunContext(processing_id=self.pid, module="intake", principal=principal or Principal.service())
        return journalled_run(self.deps, run, kind=kind, subject="UserRequest::42", event="created")


class TestJournalledRun(RunnerTestCase):
    async def test_clean_run_is_recorded_start_to_finish(self):
        async with self._run():
            pass

        run = await self.deps.journal.get(self.pid)
        self.assertEqual(run.status, "done")
        self.assertEqual(run.subject, "UserRequest::42")
        self.assertEqual(run.module, "intake")
        self.assertIsNotNone(run.finished_at)

    async def test_kind_travels_to_the_journal(self):
        async with self._run(kind="request"):
            pass

        self.assertEqual((await self.deps.journal.get(self.pid)).kind, "request")

    async def test_the_principal_travels_to_the_journal(self):
        async with self._run(principal=_ENGINEER):
            pass

        self.assertEqual((await self.deps.journal.get(self.pid)).principal, "engineer:jdoe")

    async def test_the_token_reaches_no_part_of_the_record(self):
        """Reading under an engineer's token leaves almost no trace in iTop, so
        the journal is where "who asked" lives — and must not become where their
        credential lives."""
        async with self._run(principal=_ENGINEER):
            pass

        stored = await self.deps.journal._redis.hgetall(f"run:{self.pid}")
        self.assertNotIn(_TOKEN, "".join([*stored.keys(), *stored.values()]))

    async def test_failure_is_recorded_and_re_raised(self):
        """Swallowing is the entry point's decision, not the frame's."""
        with self.assertRaises(RuntimeError):
            async with self._run():
                raise RuntimeError("boom")

        run = await self.deps.journal.get(self.pid)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "RuntimeError: boom")


if __name__ == "__main__":
    unittest.main()
