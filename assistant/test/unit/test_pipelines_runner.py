"""The outer frame of a run: opened once, closed whatever happens inside.

Both entry points share it, so it is pinned here rather than through either one.
"""

import unittest
from unittest.mock import MagicMock
from uuid import uuid4

import fakeredis.aioredis

from itop_ai_assistant.journal import RunJournal
from itop_ai_assistant.pipelines.runner import journalled_run


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = MagicMock()
        self.deps.journal = RunJournal(fakeredis.aioredis.FakeRedis(decode_responses=True))
        self.pid = uuid4()

    def _run(self, kind="webhook"):
        return journalled_run(
            self.deps, self.pid, kind=kind, subject="UserRequest::42", event="created", module="intake"
        )


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
