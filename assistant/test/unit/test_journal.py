import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fakeredis.aioredis
from redis.exceptions import RedisError

from journal import RunJournal


def _make_journal() -> tuple[RunJournal, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RunJournal(redis, ttl_seconds=3600), redis


class TestJournalLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_start_creates_running_run(self):
        journal, _ = _make_journal()
        pid = uuid4()

        await journal.start(pid, ticket="UserRequest::42", event="created", module="enrichment")

        run = await journal.get(pid)
        self.assertIsNotNone(run)
        self.assertEqual(run.status, "running")
        self.assertEqual(run.ticket, "UserRequest::42")
        self.assertEqual(run.module, "enrichment")
        self.assertIsNone(run.finished_at)

    async def test_steps_recorded_in_order(self):
        journal, _ = _make_journal()
        pid = uuid4()
        await journal.start(pid, ticket="UserRequest::42", event="created", module="enrichment")

        await journal.add_step(pid, "guard", "")
        await journal.add_step(pid, "classify", "action=ask; question=What broke?")

        run = await journal.get(pid)
        self.assertEqual([s.node for s in run.steps], ["guard", "classify"])
        self.assertIn("What broke?", run.steps[1].detail)

    async def test_finish_done(self):
        journal, _ = _make_journal()
        pid = uuid4()
        await journal.start(pid, ticket="UserRequest::42", event="created", module="enrichment")

        await journal.finish(pid, "done")

        run = await journal.get(pid)
        self.assertEqual(run.status, "done")
        self.assertIsNotNone(run.finished_at)
        self.assertIsNone(run.error)

    async def test_finish_failed_records_error(self):
        journal, _ = _make_journal()
        pid = uuid4()
        await journal.start(pid, ticket="UserRequest::42", event="created", module="enrichment")

        await journal.finish(pid, "failed", error="RuntimeError: LLM down")

        run = await journal.get(pid)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "RuntimeError: LLM down")

    async def test_get_unknown_returns_none(self):
        journal, _ = _make_journal()
        self.assertIsNone(await journal.get("no-such-id"))

    async def test_run_keys_have_ttl(self):
        journal, redis = _make_journal()
        pid = uuid4()
        await journal.start(pid, ticket="UserRequest::42", event="created", module="enrichment")
        await journal.add_step(pid, "guard")

        self.assertGreater(await redis.ttl(f"run:{pid}"), 0)
        self.assertGreater(await redis.ttl(f"run:{pid}:steps"), 0)


class TestJournalList(unittest.IsolatedAsyncioTestCase):
    async def test_list_returns_most_recent_first(self):
        journal, _ = _make_journal()
        for i in range(3):
            await journal.start(f"run-{i}", ticket=f"UserRequest::{i}", event="created", module="enrichment")

        runs = await journal.list()

        self.assertEqual([r.processing_id for r in runs], ["run-2", "run-1", "run-0"])

    async def test_list_respects_limit(self):
        journal, _ = _make_journal()
        for i in range(5):
            await journal.start(f"run-{i}", ticket="UserRequest::1", event="created", module="enrichment")

        runs = await journal.list(limit=2)

        self.assertEqual(len(runs), 2)

    async def test_list_filters_by_ticket_and_status(self):
        journal, _ = _make_journal()
        await journal.start("run-a", ticket="UserRequest::1", event="created", module="enrichment")
        await journal.start("run-b", ticket="UserRequest::2", event="created", module="enrichment")
        await journal.finish("run-b", "done")

        by_ticket = await journal.list(ticket="UserRequest::2")
        self.assertEqual([r.processing_id for r in by_ticket], ["run-b"])

        by_status = await journal.list(status="running")
        self.assertEqual([r.processing_id for r in by_status], ["run-a"])

    async def test_list_cleans_up_expired_runs(self):
        journal, redis = _make_journal()
        await journal.start("run-old", ticket="UserRequest::1", event="created", module="enrichment")
        await redis.delete("run:run-old")  # simulate TTL expiry; index entry remains

        runs = await journal.list()

        self.assertEqual(runs, [])
        self.assertEqual(await redis.zcard("runs:index"), 0)


class TestJournalNonFatalWrites(unittest.IsolatedAsyncioTestCase):
    async def test_write_methods_swallow_redis_errors(self):
        journal, redis = _make_journal()
        with (
            patch.object(redis, "pipeline", side_effect=RedisError("down")),
            patch.object(redis, "hset", AsyncMock(side_effect=RedisError("down"))),
        ):
            # must not raise — journal failures never break processing
            await journal.start("run-x", ticket="UserRequest::1", event="created", module="enrichment")
            await journal.add_step("run-x", "guard")
            await journal.finish("run-x", "done")


if __name__ == "__main__":
    unittest.main()
