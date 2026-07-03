import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

# Env/yaml on the developer machine must not leak into these tests — blank
# out every field that feeds the runtime section defaults.
_BLANK = {
    "itop_url": None,
    "itop_user": None,
    "itop_pwd": None,
    "itop_token": None,
    "llm_base_url": None,
    "llm_model": None,
    "llm_api_key": None,
    "webhook_token": None,
    "admin_token": None,
}


def _make_deps(redis, **settings_overrides) -> AppDeps:
    settings = get_settings().model_copy(update={**_BLANK, **settings_overrides})
    return AppDeps(
        settings=settings,
        itop=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=RedisConfigStore(redis, settings),
        prompt_store=RedisPromptStore(FilePromptStore(_PROMPTS_DIR), redis),
        journal=RunJournal(redis),
    )


class SetupApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.client.app.state.deps = _make_deps(self.redis)


class TestSetupStatus(SetupApiTestCase):
    def test_unconfigured_lists_missing_steps(self):
        response = self.client.get("/api/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["configured"])
        # url + credentials for iTop, base_url + model for LLM
        self.assertEqual(len(body["missing"]), 4)
        self.assertIn("itop", body["sections"])
        self.assertFalse(body["sections"]["itop"]["secrets"]["token"])

    def test_configured_after_both_sections_set(self):
        self.client.patch("/api/setup/itop", json={"url": "http://itop/rest.php", "token": "tok"})
        self.client.patch("/api/setup/llm", json={"base_url": "http://llm/v1", "model": "gpt-test"})

        body = self.client.get("/api/setup/status").json()

        self.assertTrue(body["configured"])
        self.assertEqual(body["missing"], [])
        self.assertTrue(body["sections"]["itop"]["secrets"]["token"])

    def test_env_defaults_show_through(self):
        self.client.app.state.deps = _make_deps(
            self.redis,
            itop_url="http://itop/rest.php",
            itop_token=SecretStr("t"),
            llm_base_url="http://llm/v1",
            llm_model="from-env",
        )

        body = self.client.get("/api/setup/status").json()

        self.assertTrue(body["configured"])
        self.assertEqual(body["sections"]["llm"]["values"]["model"], "from-env")


class TestSetupSections(SetupApiTestCase):
    def test_get_section_never_returns_secret_values(self):
        self.client.patch("/api/setup/itop", json={"user": "ai", "pwd": "hunter2"})

        body = self.client.get("/api/setup/itop").json()

        self.assertNotIn("pwd", body["values"])
        self.assertNotIn("hunter2", str(body))
        self.assertTrue(body["secrets"]["pwd"])
        self.assertEqual(body["values"]["user"], "ai")

    def test_patch_without_secret_keeps_stored_value(self):
        self.client.patch("/api/setup/itop", json={"user": "ai", "pwd": "hunter2"})

        # UI round-trip: form resubmitted without the password field
        response = self.client.patch("/api/setup/itop", json={"user": "ai", "url": "http://new/rest.php"})

        self.assertTrue(response.json()["secrets"]["pwd"])
        self.assertEqual(response.json()["values"]["url"], "http://new/rest.php")

    def test_patch_explicit_null_clears_secret(self):
        self.client.patch("/api/setup/itop", json={"user": "ai", "pwd": "hunter2"})

        response = self.client.patch("/api/setup/itop", json={"pwd": None})

        self.assertFalse(response.json()["secrets"]["pwd"])

    def test_patch_invalid_value_rejected(self):
        response = self.client.patch("/api/setup/itop", json={"timeout": "soon"})
        self.assertEqual(response.status_code, 422)

    def test_delete_resets_to_env_defaults(self):
        self.client.app.state.deps = _make_deps(self.redis, llm_model="env-model")
        self.client.patch("/api/setup/llm", json={"model": "runtime-model"})

        response = self.client.delete("/api/setup/llm")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/setup/llm").json()["values"]["model"], "env-model")

    def test_ticket_mapping_is_editable(self):
        response = self.client.patch("/api/setup/ticket_mapping", json={"active_statuses": ["new", "assigned"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["values"]["active_statuses"], ["new", "assigned"])

    def test_unknown_section_404(self):
        self.assertEqual(self.client.get("/api/setup/nope").status_code, 404)
        self.assertEqual(self.client.patch("/api/setup/nope", json={}).status_code, 404)


class TestAdminTokenBootstrap(SetupApiTestCase):
    def test_api_locks_after_admin_token_is_set(self):
        # First-run mode: API is open, the wizard sets a token…
        response = self.client.patch("/api/setup/security", json={"admin_token": "s3cret"})
        self.assertEqual(response.status_code, 200)

        # …after which requests without the bearer token are rejected,
        self.assertEqual(self.client.get("/api/setup/status").status_code, 401)
        # and requests with it keep working.
        response = self.client.get("/api/setup/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(response.status_code, 200)


class TestConnectionProbes(SetupApiTestCase):
    def test_itop_probe_without_url(self):
        response = self.client.post("/api/setup/test-itop", json={"token": "tok"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("URL", body["error"])

    def test_itop_probe_without_credentials(self):
        response = self.client.post("/api/setup/test-itop", json={"url": "http://itop/rest.php"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("credentials", body["error"])

    def test_itop_probe_success(self):
        client = MagicMock()
        client.schema.return_value.find_one = AsyncMock(return_value={"friendlyname": "AI Assistant"})
        client.aclose = AsyncMock()

        with patch("admin.setup.create_itop_client", return_value=client) as factory:
            response = self.client.post(
                "/api/setup/test-itop", json={"url": "http://itop/rest.php", "user": "ai", "pwd": "pw"}
            )

        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ai_person"], "AI Assistant")
        # Probe values are used for the probe only — nothing stored
        self.assertEqual(factory.call_args.args[0].user, "ai")
        self.assertFalse(self.client.get("/api/setup/itop").json()["secrets"]["pwd"])
        client.aclose.assert_awaited_once()

    def test_itop_probe_uses_stored_secret_when_absent_from_body(self):
        self.client.patch("/api/setup/itop", json={"url": "http://itop/rest.php", "user": "ai", "pwd": "stored-pw"})
        client = MagicMock()
        client.schema.return_value.find_one = AsyncMock(return_value={"friendlyname": "AI"})
        client.aclose = AsyncMock()

        with patch("admin.setup.create_itop_client", return_value=client) as factory:
            self.client.post("/api/setup/test-itop", json={"user": "ai"})

        self.assertEqual(factory.call_args.args[0].pwd, "stored-pw")

    def test_itop_probe_reports_connection_error(self):
        client = MagicMock()
        client.schema.return_value.find_one = AsyncMock(side_effect=ConnectionError("refused"))
        client.aclose = AsyncMock()

        with patch("admin.setup.create_itop_client", return_value=client):
            body = self.client.post("/api/setup/test-itop", json={"url": "http://itop/rest.php", "token": "tok"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("refused", body["error"])

    def test_itop_probe_no_person_linked(self):
        client = MagicMock()
        client.schema.return_value.find_one = AsyncMock(return_value=None)
        client.aclose = AsyncMock()

        with patch("admin.setup.create_itop_client", return_value=client):
            body = self.client.post("/api/setup/test-itop", json={"url": "http://itop/rest.php", "token": "tok"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("Person", body["error"])

    def test_llm_probe_without_base_url(self):
        body = self.client.post("/api/setup/test-llm", json={"model": "gpt-test"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("endpoint", body["error"])

    def test_llm_probe_without_model(self):
        body = self.client.post("/api/setup/test-llm", json={"base_url": "http://llm/v1"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("model", body["error"])

    def test_llm_probe_success_strips_thinking(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content="<think>hmm</think>OK"))

        with patch("admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm", json={"base_url": "http://llm/v1", "model": "gpt-test"}
            ).json()

        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["response"], "OK")

    def test_llm_probe_reports_error(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=TimeoutError("no answer"))

        with patch("admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm", json={"base_url": "http://llm/v1", "model": "gpt-test"}
            ).json()

        self.assertFalse(body["ok"])
        self.assertIn("TimeoutError", body["error"])


class TestProvisionItop(SetupApiTestCase):
    def test_requires_webhook_token(self):
        body = self.client.post(
            "/api/setup/provision-itop", json={"backend_url": "http://assistant:8000", "token": "tok"}
        ).json()

        self.assertFalse(body["ok"])
        self.assertIn("webhook token", body["error"])

    def test_requires_backend_url(self):
        self.client.patch("/api/setup/security", json={"webhook_token": "wh"})

        body = self.client.post("/api/setup/provision-itop", json={"token": "tok"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("backend_url", body["error"])

    def test_requires_admin_credentials(self):
        self.client.patch("/api/setup/security", json={"webhook_token": "wh"})

        body = self.client.post("/api/setup/provision-itop", json={"backend_url": "http://assistant:8000"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("credentials", body["error"])

    def test_happy_path_credentials_used_once_and_never_stored(self):
        self.client.patch("/api/setup/security", json={"webhook_token": "wh"})
        self.client.patch("/api/setup/itop", json={"url": "http://itop/rest.php"})
        report = [{"class": "RemoteApplicationType", "name": "iTop AI Assistant", "status": "created", "id": "1"}]
        client = MagicMock()
        client.aclose = AsyncMock()

        with (
            patch("admin.setup.create_itop_client", return_value=client) as factory,
            patch("admin.setup.provision_itop", AsyncMock(return_value=report)) as provision,
        ):
            body = self.client.post(
                "/api/setup/provision-itop",
                json={"backend_url": "http://assistant:8000", "user": "admin", "pwd": "admin-pw"},
            ).json()

        self.assertTrue(body["ok"])
        self.assertEqual(body["report"], report)
        provision.assert_awaited_once_with(client, "http://assistant:8000", "wh")
        # Admin credentials go into the one-off client (url from the stored
        # section) and never reach the config store.
        self.assertEqual(factory.call_args.args[0].user, "admin")
        self.assertEqual(factory.call_args.args[0].url, "http://itop/rest.php")
        itop_section = self.client.get("/api/setup/itop").json()
        self.assertIsNone(itop_section["values"]["user"])
        self.assertFalse(itop_section["secrets"]["pwd"])
        client.aclose.assert_awaited_once()

    def test_provision_error_reported(self):
        self.client.patch("/api/setup/security", json={"webhook_token": "wh"})
        client = MagicMock()
        client.aclose = AsyncMock()

        with (
            patch("admin.setup.create_itop_client", return_value=client),
            patch("admin.setup.provision_itop", AsyncMock(side_effect=ConnectionError("refused"))),
        ):
            body = self.client.post(
                "/api/setup/provision-itop", json={"backend_url": "http://assistant:8000", "token": "tok"}
            ).json()

        self.assertFalse(body["ok"])
        self.assertIn("refused", body["error"])
        client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
