import unittest
from importlib.metadata import version as metadata_version
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import SecretStr

from itop_ai_assistant.agents.intake.prompts import PROMPTS_DIR as INTAKE_PROMPTS_DIR
from itop_ai_assistant.agents.selfcheck.prompts import PROMPTS_DIR as SELFCHECK_PROMPTS_DIR
from itop_ai_assistant.config import get_settings
from itop_ai_assistant.content_sources.registry import build_vector_sources
from itop_ai_assistant.core.deps import AppDeps
from itop_ai_assistant.main import app
from itop_ai_assistant.pipelines.registry import ModuleInfo, ScheduleRoute, TriggerRegistry
from itop_ai_assistant.settings.config_store import RedisConfigStore
from itop_ai_assistant.settings.prompt_store import FilePromptStore, RedisPromptStore
from itop_ai_assistant.state.journal import RunJournal
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.util.build_info import get_build_info
from itop_ai_assistant.vector.adapters.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.assembly import VectorSubsystem
from itop_ai_assistant.vector.state.index_journal import IndexJournal
from itop_ai_assistant.vector.state.sync_state import VectorSyncState
from itop_ai_assistant.vector.use_cases.search import SimilarSearch


def _make_deps(redis, settings=None) -> AppDeps:
    settings = settings or get_settings()
    config_store = RedisConfigStore(redis, settings)
    itop = MagicMock()
    vector_store = QdrantChunkStore(None)

    def vector_sources(cfg):
        return build_vector_sources(itop, cfg)

    vector = VectorSubsystem(
        config_store=config_store,
        itop=itop,
        vector_store=vector_store,
        vector_search=SimilarSearch(vector_store, config_store, build_sources=vector_sources),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
        vector_sources=vector_sources,
    )

    return AppDeps(
        settings=settings,
        itop=itop,
        itop_connection=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=config_store,
        prompt_store=RedisPromptStore(
            FilePromptStore({"intake": INTAKE_PROMPTS_DIR, "selfcheck": SELFCHECK_PROMPTS_DIR}), redis
        ),
        journal=RunJournal(redis),
        vector=vector,
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


class TestVersion(AdminApiTestCase):
    def test_serves_the_baked_build_stamp(self):
        response = self.client.get("/version")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"version", "commit", "built_at"})
        # Whatever the build baked in — asserting a literal would pin the test
        # to one release. It must agree with the distribution metadata.
        self.assertEqual(body["version"], get_build_info().version)
        self.assertEqual(body["version"], metadata_version("itop-ai-assistant"))


class TestModules(AdminApiTestCase):
    def test_lists_intake_module(self):
        response = self.client.get("/api/modules")

        self.assertEqual(response.status_code, 200)
        modules = response.json()
        self.assertEqual(modules[0]["name"], "intake")
        self.assertTrue(modules[0]["has_config"])
        self.assertIn("system", modules[0]["prompts"])

    def test_lists_request_actions_with_their_schema(self):
        """The UI builds the form from this, so the schema has to travel with it."""
        modules = self.client.get("/api/modules").json()

        (action,) = modules[0]["requests"]
        self.assertEqual(action["action"], "process")
        self.assertTrue(action["summary"])
        self.assertEqual(sorted(action["input_schema"]["required"]), ["class", "id"])

    def test_lists_schedules(self):
        """A module's own triggers, whatever kind — intake has none on a timer."""
        registry = TriggerRegistry()
        registry.register(
            ModuleInfo(name="probe", description="Probe"),
            schedules=[
                ScheduleRoute(
                    name="tick",
                    module="probe",
                    handler=AsyncMock(),
                    interval_of=AsyncMock(return_value=60.0),
                    default_interval=120.0,
                    summary="Probe tick",
                )
            ],
        )
        original = self.client.app.state.registry
        self.client.app.state.registry = registry
        self.addCleanup(setattr, self.client.app.state, "registry", original)

        (module,) = self.client.get("/api/modules").json()

        self.assertEqual(module["requests"], [])
        (schedule,) = module["schedules"]
        self.assertEqual(schedule["name"], "tick")
        self.assertEqual(schedule["summary"], "Probe tick")
        self.assertEqual(schedule["default_interval"], 120.0)


class TestConfigEndpoints(AdminApiTestCase):
    def test_get_config_returns_defaults(self):
        response = self.client.get("/api/config/intake")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_rounds"], 2)

    def test_put_config_applies_from_next_read(self):
        response = self.client.put("/api/config/intake", json={"max_rounds": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_rounds"], 5)
        self.assertEqual(self.client.get("/api/config/intake").json()["max_rounds"], 5)

    def test_put_invalid_config_rejected(self):
        response = self.client.put("/api/config/intake", json={"max_rounds": "many"})

        self.assertEqual(response.status_code, 422)
        # Nothing stored
        self.assertEqual(self.client.get("/api/config/intake").json()["max_rounds"], 2)

    def test_delete_resets_to_defaults(self):
        self.client.put("/api/config/intake", json={"max_rounds": 5})

        response = self.client.delete("/api/config/intake")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/config/intake").json()["max_rounds"], 2)

    def test_schema_returned(self):
        response = self.client.get("/api/config/intake/schema")

        self.assertEqual(response.status_code, 200)
        self.assertIn("max_rounds", response.json()["properties"])

    def test_unknown_module_404(self):
        self.assertEqual(self.client.get("/api/config/nope").status_code, 404)


class TestPromptEndpoints(AdminApiTestCase):
    def test_get_prompts(self):
        response = self.client.get("/api/prompts/intake")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("system", body["prompts"])
        self.assertEqual(body["overridden"], [])

    def test_put_prompt_and_reset(self):
        new_text = "You are an intake assistant. Requester: {caller_name}."
        response = self.client.put("/api/prompts/intake/ticket_human", json={"text": new_text})
        self.assertEqual(response.status_code, 200)

        body = self.client.get("/api/prompts/intake").json()
        self.assertEqual(body["prompts"]["ticket_human"], new_text)
        self.assertEqual(body["overridden"], ["ticket_human"])

        self.assertEqual(self.client.delete("/api/prompts/intake/ticket_human").status_code, 204)
        body = self.client.get("/api/prompts/intake").json()
        self.assertEqual(body["overridden"], [])

    def test_put_prompt_with_unknown_placeholder_rejected(self):
        response = self.client.put("/api/prompts/intake/ticket_human", json={"text": "Hello {caler_name}"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("caler_name", response.json()["detail"])
        # Nothing stored
        self.assertEqual(self.client.get("/api/prompts/intake").json()["overridden"], [])

    def test_put_unknown_prompt_404(self):
        response = self.client.put("/api/prompts/intake/no_such", json={"text": "x"})
        self.assertEqual(response.status_code, 404)


class TestRunEndpoints(AdminApiTestCase):
    def _seed_runs(self):
        async def seed():
            await self.deps.journal.start("run-1", subject="UserRequest::1", event="created", module="intake")
            await self.deps.journal.add_step("run-1", "guard", "")
            await self.deps.journal.finish("run-1", "done")
            await self.deps.journal.start("run-2", subject="UserRequest::2", event="created", module="intake")

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

    def test_list_runs_filtered_by_subject(self):
        self._seed_runs()

        runs = self.client.get("/api/runs", params={"subject": "UserRequest::2"}).json()

        self.assertEqual([r["processing_id"] for r in runs], ["run-2"])
        self.assertEqual(runs[0]["subject"], "UserRequest::2")

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

    def test_version_is_public(self):
        self.assertEqual(self.client.get("/version").status_code, 200)


if __name__ == "__main__":
    unittest.main()
