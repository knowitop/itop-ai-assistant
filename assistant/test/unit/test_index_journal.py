import unittest

import fakeredis.aioredis

from itop_ai_assistant.vector.index_journal import MAX_ENTRIES, IndexJournal


def _make_journal() -> IndexJournal:
    return IndexJournal(fakeredis.aioredis.FakeRedis(decode_responses=True))


class TestIndexJournal(unittest.IsolatedAsyncioTestCase):
    async def test_started_run_is_visible_as_running(self):
        journal = _make_journal()

        await journal.start("sweep")

        entries = await journal.recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "sweep")
        self.assertEqual(entries[0]["status"], "running")
        self.assertIsNone(entries[0]["finished_at"])

    async def test_finish_records_counters(self):
        journal = _make_journal()
        entry_id = await journal.start("backfill")

        await journal.finish(entry_id, status="ok", objects_seen=7, chunks_embedded=3, chunks_deleted=1)

        entry = (await journal.recent())[0]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["objects_seen"], 7)
        self.assertEqual(entry["chunks_embedded"], 3)
        self.assertEqual(entry["chunks_deleted"], 1)
        self.assertIsNotNone(entry["finished_at"])

    async def test_newest_first(self):
        journal = _make_journal()
        await journal.start("sweep")
        await journal.start("reconcile")

        self.assertEqual([e["kind"] for e in await journal.recent()], ["reconcile", "sweep"])

    async def test_history_is_capped(self):
        journal = _make_journal()
        for _ in range(MAX_ENTRIES + 5):
            await journal.start("sweep")

        self.assertEqual(len(await journal.recent(limit=MAX_ENTRIES + 10)), MAX_ENTRIES)

    async def test_finishing_an_evicted_entry_is_not_an_error(self):
        # A backfill can outlive its own journal entry; the sweep must not die of it
        journal = _make_journal()

        await journal.finish("nonexistent", status="ok")
