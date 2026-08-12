"""The synchronous trigger: same registry, same shell, same journal as a webhook —
but the caller waits for the outcome and sees failures.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
from fastapi.testclient import TestClient

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.selfcheck.config import SelfCheckConfig
from itop_ai_assistant.config import (
    ItopConfig,
    LlmConfig,
    SecurityConfig,
    TicketMappingConfig,
)
from itop_ai_assistant.main import app
from itop_ai_assistant.pipelines.models import ObjectRef, RunOutcome
from itop_ai_assistant.pipelines.registry import ModuleInfo, RequestRoute, build_registry
from itop_ai_assistant.state.journal import RunJournal


def _mock_deps(security: SecurityConfig | None = None, configured: bool = True) -> MagicMock:
    """AppDeps double with a real journal on fakeredis — the run trace is asserted."""
    sections = {
        "security": security or SecurityConfig(),
        "itop": ItopConfig(url="http://itop/rest.php", token="tok") if configured else ItopConfig(),
        "llm": LlmConfig(base_url="http://llm/v1", model="test-model") if configured else LlmConfig(),
        "ticket_mapping": TicketMappingConfig(),
    }

    deps = MagicMock()
    deps.config_store.get = AsyncMock(side_effect=lambda module, model: sections[module])
    deps.journal = RunJournal(fakeredis.aioredis.FakeRedis(decode_responses=True))
    deps.state_manager.acquire_lock = AsyncMock(return_value=True)
    deps.state_manager.release_lock = AsyncMock()
    repos = MagicMock()
    # "not found" — a deterministic outcome that needs no LLM
    repos.ticket_repo.fetch = AsyncMock(return_value=None)
    deps.itop.for_principal = AsyncMock(return_value=repos)
    return deps


class RequestApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.deps = _mock_deps()
        self.client.app.state.deps = self.deps

    async def _runs(self) -> list:
        return await self.deps.journal.list()


class TestIntakeProcessNow(RequestApiTestCase):
    def test_runs_the_ticket_and_returns_the_outcome(self):
        response = self.client.post("/api/modules/intake/process", json={"class": "UserRequest", "id": "123"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "skipped")
        self.assertEqual(body["detail"], "ticket not found in iTop")
        self.assertTrue(body["processing_id"])

    def test_run_is_journalled_as_a_request(self):
        response = self.client.post("/api/modules/intake/process", json={"class": "UserRequest", "id": "123"})

        run = self.client.get(f"/api/runs/{response.json()['processing_id']}").json()
        self.assertEqual(run["kind"], "request")
        self.assertEqual(run["module"], "intake")
        self.assertEqual(run["event"], "process")
        self.assertEqual(run["subject"], "UserRequest::123")
        self.assertEqual(run["status"], "done")

    def test_unknown_action_is_404(self):
        response = self.client.post("/api/modules/intake/nope", json={"class": "UserRequest", "id": "1"})

        self.assertEqual(response.status_code, 404)
        self.assertIn("intake/nope", response.json()["detail"])

    def test_unknown_module_is_404(self):
        response = self.client.post("/api/modules/nope/process", json={"class": "UserRequest", "id": "1"})

        self.assertEqual(response.status_code, 404)

    def test_invalid_body_is_422(self):
        response = self.client.post("/api/modules/intake/process", json={"class": "UserRequest"})

        self.assertEqual(response.status_code, 422)


class TestProbeModule(unittest.TestCase):
    """The endpoint is generic: it knows a registry entry, not intake."""

    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.client.app.state.deps = _mock_deps()

        self.calls: list[ObjectRef] = []
        self.raises: Exception | None = None
        _configs = {"intake": IntakeConfig(), "selfcheck": SelfCheckConfig()}
        registry = build_registry(SimpleNamespace(module_defaults=lambda name, model: _configs[name]))
        registry.register(
            ModuleInfo(name="probe", description="Probe"),
            requests=[
                RequestRoute(
                    action="run",
                    module="probe",
                    input_model=ObjectRef,
                    handler=self._handler,
                    subject_of=lambda ref: ref.label,
                    summary="Probe action",
                )
            ],
        )
        original = self.client.app.state.registry
        self.client.app.state.registry = registry
        self.addCleanup(setattr, self.client.app.state, "registry", original)

    async def _handler(self, ref, run, deps) -> RunOutcome:
        self.calls.append(ref)
        if self.raises:
            raise self.raises
        return RunOutcome(status="done", detail="probe ran")

    def test_handler_gets_the_validated_input_model(self):
        response = self.client.post("/api/modules/probe/run", json={"class": "Change", "id": "9"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["detail"], "probe ran")
        self.assertEqual([ref.label for ref in self.calls], ["Change::9"])

    def test_handler_failure_reaches_the_caller_and_the_journal(self):
        """Unlike a webhook, nobody else is watching — the caller must be told."""
        self.raises = RuntimeError("boom")
        client = TestClient(self.client.app, raise_server_exceptions=False)

        response = client.post("/api/modules/probe/run", json={"class": "Change", "id": "9"})

        self.assertEqual(response.status_code, 500)
        runs = self.client.get("/api/runs").json()
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIn("boom", runs[0]["error"])


class TestRequestAuth(unittest.TestCase):
    PAYLOAD = {"class": "UserRequest", "id": "123"}

    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.client.app.state.deps = _mock_deps(security=SecurityConfig(admin_token="test-secret"))

    def test_missing_token_rejected(self):
        response = self.client.post("/api/modules/intake/process", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 401)

    def test_correct_token_accepted(self):
        response = self.client.post(
            "/api/modules/intake/process",
            json=self.PAYLOAD,
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 200)


class TestRequestNotConfigured(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.client.app.state.deps = _mock_deps(configured=False)

    def test_disabled_until_setup_complete(self):
        response = self.client.post("/api/modules/intake/process", json={"class": "UserRequest", "id": "123"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
