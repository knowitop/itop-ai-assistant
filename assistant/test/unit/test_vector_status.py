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
from itop_ai_assistant.vector.db import VectorDb
from itop_ai_assistant.vector.index import VectorIndex
from itop_ai_assistant.vector.index_journal import IndexJournal
from itop_ai_assistant.vector.indexer import SWEEP_TASK
from itop_ai_assistant.vector.sync_state import VectorSyncState

_BLANK = {
    "admin_token": None,
    "embeddings_base_url": None,
    "embeddings_model": None,
    "embeddings_api_key": None,
    "qdrant_url": None,
}


def _make_deps(redis, store_dsn: str | None = None, **settings_overrides) -> AppDeps:
    """`store_dsn` feeds a pgvector-backed `ChunkStore` double, not the
    production `qdrant_url` setting — these tests exercise `vector/router.py`
    against the `ChunkStore` port generically (configured-but-unreachable
    included), independent of which backend is actually wired."""
    settings = get_settings().model_copy(update={**_BLANK, **settings_overrides})
    return AppDeps(
        settings=settings,
        itop=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=RedisConfigStore(redis, settings),
        prompt_store=RedisPromptStore(FilePromptStore(PACKAGED_PROMPTS_DIR), redis),
        journal=RunJournal(redis),
        vector_store=VectorIndex(VectorDb(store_dsn)),
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
        self.client.app.state.deps = _make_deps(self.redis, store_dsn="postgresql+asyncpg://localhost:1/x")

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
        self.client.app.state.deps = _make_deps(self.redis, store_dsn="postgresql+asyncpg://localhost:1/x")

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 409)
        self.assertIn("disabled", response.json()["detail"])

    def test_503_when_the_request_cannot_be_stored(self):
        """The mark is a flag in Redis now, so an unreachable Redis is a real
        failure — it must say so instead of pretending the backfill was
        scheduled."""
        deps = _make_deps(self.redis, store_dsn="postgresql+asyncpg://localhost:1/x")
        deps.vector_sync.request_reindex = AsyncMock(side_effect=ConnectionError("redis down"))
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["detail"])

    def test_202_marks_the_request_and_wakes_the_sweep(self):
        """The request is a flag in Redis — the wake-up only makes the local
        loop act on it sooner."""
        deps = _make_deps(self.redis, store_dsn="postgresql+asyncpg://localhost:1/x")
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
