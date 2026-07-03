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

_PROMPTS_DIR = Path(__file__).parents[2] / "prompts"


def _make_deps(redis, settings=None) -> AppDeps:
    settings = settings or get_settings()
    return AppDeps(
        settings=settings,
        itop=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=RedisConfigStore(redis, settings),
        prompt_store=RedisPromptStore(FilePromptStore(_PROMPTS_DIR), redis),
        journal=RunJournal(redis),
    )


class AdminApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.deps = _make_deps(self.redis)
        self.client.app.state.deps = self.deps


class TestHealth(AdminApiTestCase):
    def test_health_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "redis": True})


class TestModules(AdminApiTestCase):
    def test_lists_enrichment_module(self):
        response = self.client.get("/api/modules")

        self.assertEqual(response.status_code, 200)
        modules = response.json()
        self.assertEqual(modules[0]["name"], "enrichment")
        self.assertTrue(modules[0]["has_config"])
        self.assertIn("evaluate_system", modules[0]["prompts"])


class TestConfigEndpoints(AdminApiTestCase):
    def test_get_config_returns_defaults(self):
        response = self.client.get("/api/config/enrichment")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_rounds"], 2)

    def test_put_config_applies_from_next_read(self):
        response = self.client.put("/api/config/enrichment", json={"max_rounds": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_rounds"], 5)
        self.assertEqual(self.client.get("/api/config/enrichment").json()["max_rounds"], 5)

    def test_put_invalid_config_rejected(self):
        response = self.client.put("/api/config/enrichment", json={"max_rounds": "many"})

        self.assertEqual(response.status_code, 422)
        # Nothing stored
        self.assertEqual(self.client.get("/api/config/enrichment").json()["max_rounds"], 2)

    def test_delete_resets_to_defaults(self):
        self.client.put("/api/config/enrichment", json={"max_rounds": 5})

        response = self.client.delete("/api/config/enrichment")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/config/enrichment").json()["max_rounds"], 2)

    def test_schema_returned(self):
        response = self.client.get("/api/config/enrichment/schema")

        self.assertEqual(response.status_code, 200)
        self.assertIn("max_rounds", response.json()["properties"])

    def test_unknown_module_404(self):
        self.assertEqual(self.client.get("/api/config/nope").status_code, 404)


class TestPromptEndpoints(AdminApiTestCase):
    def test_get_prompts(self):
        response = self.client.get("/api/prompts/enrichment")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("evaluate_system", body["prompts"])
        self.assertEqual(body["overridden"], [])

    def test_put_prompt_and_reset(self):
        new_text = "You are an intake assistant. Requester: {caller_name}."
        response = self.client.put("/api/prompts/enrichment/enrich_system", json={"text": new_text})
        self.assertEqual(response.status_code, 200)

        body = self.client.get("/api/prompts/enrichment").json()
        self.assertEqual(body["prompts"]["enrich_system"], new_text)
        self.assertEqual(body["overridden"], ["enrich_system"])

        self.assertEqual(self.client.delete("/api/prompts/enrichment/enrich_system").status_code, 204)
        body = self.client.get("/api/prompts/enrichment").json()
        self.assertEqual(body["overridden"], [])

    def test_put_prompt_with_unknown_placeholder_rejected(self):
        response = self.client.put("/api/prompts/enrichment/enrich_system", json={"text": "Hello {caler_name}"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("caler_name", response.json()["detail"])
        # Nothing stored
        self.assertEqual(self.client.get("/api/prompts/enrichment").json()["overridden"], [])

    def test_put_unknown_prompt_404(self):
        response = self.client.put("/api/prompts/enrichment/no_such", json={"text": "x"})
        self.assertEqual(response.status_code, 404)


class TestRunEndpoints(AdminApiTestCase):
    def _seed_runs(self):
        async def seed():
            await self.deps.journal.start("run-1", ticket="UserRequest::1", event="created", module="enrichment")
            await self.deps.journal.add_step("run-1", "guard", "")
            await self.deps.journal.finish("run-1", "done")
            await self.deps.journal.start("run-2", ticket="UserRequest::2", event="created", module="enrichment")

        self.client.portal.call(seed)  # run inside the TestClient event loop

    def test_list_runs(self):
        self._seed_runs()

        response = self.client.get("/api/runs")

        self.assertEqual(response.status_code, 200)
        runs = response.json()
        self.assertEqual([r["processing_id"] for r in runs], ["run-2", "run-1"])

    def test_list_runs_filtered_by_status(self):
        self._seed_runs()

        runs = self.client.get("/api/runs", params={"status": "done"}).json()

        self.assertEqual([r["processing_id"] for r in runs], ["run-1"])

    def test_get_run_with_steps(self):
        self._seed_runs()

        response = self.client.get("/api/runs/run-1")

        self.assertEqual(response.status_code, 200)
        run = response.json()
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["steps"][0]["node"], "guard")

    def test_get_unknown_run_404(self):
        self.assertEqual(self.client.get("/api/runs/nope").status_code, 404)


class TestAdminAuth(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = get_settings().model_copy(update={"admin_token": SecretStr("admin-secret")})
        self.client.app.state.deps = _make_deps(redis, settings)

    def test_missing_token_rejected(self):
        response = self.client.get("/api/modules")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_wrong_token_rejected(self):
        response = self.client.get("/api/modules", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)

    def test_non_bearer_scheme_rejected(self):
        response = self.client.get("/api/modules", headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
        self.assertEqual(response.status_code, 401)

    def test_correct_token_accepted(self):
        response = self.client.get("/api/modules", headers={"Authorization": "Bearer admin-secret"})
        self.assertEqual(response.status_code, 200)

    def test_health_is_public(self):
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
