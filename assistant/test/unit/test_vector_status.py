import unittest
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import SecretStr

from config import get_settings
from config_store import RedisConfigStore
from deps import AppDeps
from journal import RunJournal
from main import app
from prompt_store import FilePromptStore, RedisPromptStore
from state.ticket_state import TicketStateManager
from vector.db import VectorDb

_PROMPTS_DIR = Path(__file__).parents[2] / "prompts"

_BLANK = {
    "admin_token": None,
    "embeddings_base_url": None,
    "embeddings_model": None,
    "embeddings_api_key": None,
    "database_url": None,
}


def _make_deps(redis, database_url: str | None = None, **settings_overrides) -> AppDeps:
    settings = get_settings().model_copy(update={**_BLANK, **settings_overrides})
    return AppDeps(
        settings=settings,
        itop=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=RedisConfigStore(redis, settings),
        prompt_store=RedisPromptStore(FilePromptStore(_PROMPTS_DIR), redis),
        journal=RunJournal(redis),
        vector_db=VectorDb(database_url),
    )


class VectorStatusTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.client.app.state.deps = _make_deps(self.redis)


class TestVectorStatus(VectorStatusTestCase):
    def test_unconfigured_database(self):
        body = self.client.get("/api/vector/status").json()

        self.assertFalse(body["enabled"])
        self.assertFalse(body["embeddings_configured"])
        self.assertFalse(body["database"]["configured"])
        self.assertIsNone(body["database"]["ok"])
        self.assertIsNone(body["index"])

    def test_database_down_reports_error_not_500(self):
        # Port 1 is never listening — connection fails fast
        self.client.app.state.deps = _make_deps(self.redis, database_url="postgresql+asyncpg://localhost:1/x")

        response = self.client.get("/api/vector/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["database"]["configured"])
        self.assertFalse(body["database"]["ok"])
        self.assertTrue(body["database"]["error"])
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


if __name__ == "__main__":
    unittest.main()
