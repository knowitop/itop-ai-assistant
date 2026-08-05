import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import SecretStr

from itop_ai_assistant.config import get_settings
from itop_ai_assistant.config_store import RedisConfigStore
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.journal import RunJournal
from itop_ai_assistant.main import app
from itop_ai_assistant.prompt_store import PACKAGED_PROMPTS_DIR, FilePromptStore, RedisPromptStore
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.vector.index_journal import IndexJournal
from itop_ai_assistant.vector.indexer import SWEEP_TASK
from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.sync_state import VectorSyncState

_BLANK = {
    "admin_token": None,
    "embeddings_base_url": None,
    "embeddings_model": None,
    "embeddings_api_key": None,
    "qdrant_url": None,
}


def _make_deps(redis, store_url: str | None = None, **settings_overrides) -> AppDeps:
    """`store_url` feeds the `ChunkStore` directly, not the production
    `qdrant_url` setting — these tests exercise `vector/router.py` against the
    `ChunkStore` port itself (configured-but-unreachable included)."""
    settings = get_settings().model_copy(update={**_BLANK, **settings_overrides})
    return AppDeps(
        settings=settings,
        itop=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=RedisConfigStore(redis, settings),
        prompt_store=RedisPromptStore(FilePromptStore(PACKAGED_PROMPTS_DIR), redis),
        journal=RunJournal(redis),
        vector_store=QdrantChunkStore(store_url),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
    )


class VectorStatusTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.client.app.state.deps = _make_deps(self.redis)
        # The lifespan built a real (empty) scheduler: no qdrant_url in the
        # test settings, so the sweep loop was never registered
        self.tasks = self.client.app.state.tasks


class TestVectorStatus(VectorStatusTestCase):
    def test_unconfigured_database(self):
        body = self.client.get("/api/vector/status").json()

        self.assertFalse(body["enabled"])
        self.assertFalse(body["embeddings_configured"])
        self.assertFalse(body["store"]["configured"])
        self.assertIsNone(body["store"]["ok"])
        self.assertIsNone(body["index"])
        self.assertIsNone(body["sync"])
        self.assertIsNone(body["last_reconcile"])
        self.assertEqual(body["runs"], [])
        self.assertFalse(body["indexer_running"])

    def test_database_down_reports_error_not_500(self):
        # Port 1 is never listening — connection fails fast
        self.client.app.state.deps = _make_deps(self.redis, store_url="http://localhost:1")

        response = self.client.get("/api/vector/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["store"]["configured"])
        self.assertFalse(body["store"]["ok"])
        self.assertTrue(body["store"]["error"])
        self.assertIsNone(body["index"])

    def test_embeddings_configured_flag(self):
        self.client.app.state.deps = _make_deps(
            self.redis, embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )

        body = self.client.get("/api/vector/status").json()

        self.assertTrue(body["embeddings_configured"])

    def test_enabled_reflects_vector_section(self):
        self.client.patch("/api/setup/vector", json={"enabled": True})

        body = self.client.get("/api/vector/status").json()

        self.assertTrue(body["enabled"])

    def test_index_is_a_list_of_families(self):
        # `tickets` is always in the registry (registry.py), so it appears
        # `configured: true` even with no active version yet — same "no
        # index" case the old single-block response used to report.
        deps = _make_deps(self.redis, store_url=":memory:")
        self.client.app.state.deps = deps

        body = self.client.get("/api/vector/status").json()

        self.assertEqual(len(body["index"]), 1)
        entry = body["index"][0]
        self.assertEqual(entry["family"], "tickets")
        self.assertTrue(entry["configured"])
        self.assertIsNone(entry["active_version"])
        self.assertIsNone(entry["rows"])

    def test_a_family_no_longer_in_the_registry_is_reported_as_unconfigured(self):
        # list_families() (Qdrant) knows about `kb_articles`; build_vector_sources()
        # (code) does not — the union still surfaces it, flagged instead of dropped.
        deps = _make_deps(self.redis, store_url=":memory:")
        asyncio.run(deps.vector_store.ensure_version("kb_articles", "bge-m3", 4))
        self.client.app.state.deps = deps

        body = self.client.get("/api/vector/status").json()

        families = {entry["family"]: entry for entry in body["index"]}
        self.assertEqual(set(families), {"tickets", "kb_articles"})
        self.assertTrue(families["tickets"]["configured"])
        self.assertFalse(families["kb_articles"]["configured"])
        self.assertEqual(families["kb_articles"]["active_version"], 1)
        self.assertEqual(families["kb_articles"]["model"], "bge-m3")
        self.assertEqual(families["kb_articles"]["rows"], 0)

    def test_requires_admin_token_when_set(self):
        self.client.app.state.deps = _make_deps(self.redis, admin_token=SecretStr("s3cret"))

        self.assertEqual(self.client.get("/api/vector/status").status_code, 401)
        response = self.client.get("/api/vector/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(response.status_code, 200)


class TestReindex(VectorStatusTestCase):
    def test_409_when_database_not_configured(self):
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 409)
        self.assertIn("qdrant_url", response.json()["detail"])

    def test_409_when_vector_disabled(self):
        self.client.app.state.deps = _make_deps(self.redis, store_url="http://localhost:1")

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 409)
        self.assertIn("disabled", response.json()["detail"])

    def test_503_when_the_request_cannot_be_stored(self):
        """The mark is a flag in Redis now, so an unreachable Redis is a real
        failure — it must say so instead of pretending the backfill was
        scheduled."""
        deps = _make_deps(self.redis, store_url="http://localhost:1")
        deps.vector_sync.request_reindex = AsyncMock(side_effect=ConnectionError("redis down"))
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["detail"])

    def test_202_marks_the_request_and_wakes_the_sweep(self):
        """The request is a flag in Redis — the wake-up only makes the local
        loop act on it sooner."""
        deps = _make_deps(self.redis, store_url="http://localhost:1")
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        self.tasks.wake = MagicMock(return_value=True)

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "scheduled"})
        self.assertTrue(asyncio.run(deps.vector_sync.reindex_pending()))
        self.tasks.wake.assert_called_once_with(SWEEP_TASK)


if __name__ == "__main__":
    unittest.main()
