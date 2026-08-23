import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs

import fakeredis.aioredis
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from itop_ai_assistant.agents.intake.prompts import PROMPTS_DIR as INTAKE_PROMPTS_DIR
from itop_ai_assistant.agents.selfcheck.prompts import PROMPTS_DIR as SELFCHECK_PROMPTS_DIR
from itop_ai_assistant.config import get_settings
from itop_ai_assistant.content_sources.registry import build_vector_sources
from itop_ai_assistant.core.deps import AppDeps
from itop_ai_assistant.core.tracing import NullRunTracer
from itop_ai_assistant.itop.write_policy import WritePolicy
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.main import app
from itop_ai_assistant.settings.config_store import RedisConfigStore
from itop_ai_assistant.settings.prompt_store import FilePromptStore, RedisPromptStore
from itop_ai_assistant.state.counters import DailyCounters
from itop_ai_assistant.state.journal import RunJournal
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.vector.adapters.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.assembly import VectorSubsystem
from itop_ai_assistant.vector.state.index_journal import IndexJournal
from itop_ai_assistant.vector.state.sync_state import VectorSyncState
from itop_ai_assistant.vector.use_cases.search import SimilarSearch

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
    "embeddings_base_url": None,
    "embeddings_model": None,
    "embeddings_api_key": None,
    "llm_provider": "openai_compatible",
    "llm_supports_forced_tool_choice": None,
}


def _fake_llm(content: str, tool_calls: list | None = None, tool_error: Exception | None = None) -> MagicMock:
    """An LLM stand-in for the two-step probe, recording how tools were bound."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content=content))
    llm.bind_kwargs = []
    bound = MagicMock()
    if tool_error is not None:
        bound.ainvoke = AsyncMock(side_effect=tool_error)
    else:
        calls = [{"name": "_probe_tool", "args": {"text": "ping"}, "id": "1"}] if tool_calls is None else tool_calls
        bound.ainvoke = AsyncMock(return_value=MagicMock(tool_calls=calls))

    def bind_tools(_tools, **kwargs):
        llm.bind_kwargs.append(kwargs)
        return bound

    llm.bind_tools = bind_tools
    return llm


def _make_deps(redis, **settings_overrides) -> AppDeps:
    settings = get_settings().model_copy(update={**_BLANK, **settings_overrides})
    config_store = RedisConfigStore(redis, settings)
    itop = MagicMock()
    vector_store = QdrantChunkStore(None)

    def vector_sources(cfg):
        return build_vector_sources(itop, cfg)

    counters = DailyCounters(redis)
    vector = VectorSubsystem(
        config_store=config_store,
        itop=itop,
        vector_store=vector_store,
        vector_search=SimilarSearch(vector_store, config_store, vector_sources, counters),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
        vector_sources=vector_sources,
    )

    return AppDeps(
        settings=settings,
        itop=itop,
        itop_connection=MagicMock(),
        write_policy=WritePolicy(config_store),
        state_manager=TicketStateManager(redis),
        config_store=config_store,
        prompt_store=RedisPromptStore(
            FilePromptStore({"intake": INTAKE_PROMPTS_DIR, "selfcheck": SELFCHECK_PROMPTS_DIR}), redis
        ),
        journal=RunJournal(redis),
        counters=counters,
        tracer=NullRunTracer(),
        vector=vector,
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


class TestLlmProviders(SetupApiTestCase):
    def test_registry_is_served_for_the_ui(self):
        providers = {p["id"]: p for p in self.client.get("/api/setup/llm-providers").json()["providers"]}

        self.assertIn("openai_compatible", providers)
        self.assertEqual(providers["google_genai"]["base_url_mode"], "unused")
        self.assertEqual(providers["google_genai"]["api_key_mode"], "required")

    def test_only_openai_compatible_leaves_the_tool_choice_question_open(self):
        # null is the UI's signal to show the toggle — everywhere else the
        # answer is known and asking would invite the user to contradict it
        providers = self.client.get("/api/setup/llm-providers").json()["providers"]
        open_question = [p["id"] for p in providers if p["supports_forced_tool_choice"] is None]

        self.assertEqual(open_question, ["openai_compatible"])


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
        response = self.client.patch(
            "/api/setup/ticket_mapping", json={"class_overrides": {"Incident": {"title": None}}}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["values"]["class_overrides"], {"Incident": {"title": None}})

    def test_embeddings_section_masks_api_key(self):
        self.client.patch("/api/setup/embeddings", json={"base_url": "http://emb/v1", "api_key": "sk-emb"})

        body = self.client.get("/api/setup/embeddings").json()

        self.assertNotIn("api_key", body["values"])
        self.assertNotIn("sk-emb", str(body))
        self.assertTrue(body["secrets"]["api_key"])
        self.assertEqual(body["values"]["base_url"], "http://emb/v1")

    def test_embeddings_patch_and_reset(self):
        self.client.patch("/api/setup/embeddings", json={"model": "bge-m3", "dimension": 768})
        self.assertEqual(self.client.get("/api/setup/embeddings").json()["values"]["dimension"], 768)

        response = self.client.delete("/api/setup/embeddings")

        self.assertEqual(response.status_code, 204)
        body = self.client.get("/api/setup/embeddings").json()
        self.assertIsNone(body["values"]["model"])
        self.assertEqual(body["values"]["dimension"], 1024)

    def test_embeddings_invalid_dimension_rejected(self):
        response = self.client.patch("/api/setup/embeddings", json={"dimension": 4097})
        self.assertEqual(response.status_code, 422)

    def test_vector_section_is_editable(self):
        families = {
            "tickets": {
                "classes": {
                    "UserRequest": {"index_values": ["resolved"], "chunks": {"body": {"fields": ["description"]}}}
                }
            }
        }
        response = self.client.patch("/api/setup/vector", json={"enabled": True, "families": families})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["values"]["enabled"])
        saved = response.json()["values"]["families"]["tickets"]["classes"]["UserRequest"]
        self.assertEqual(saved["index_values"], ["resolved"])
        self.assertEqual(saved["chunks"]["body"], {"fields": ["description"], "enabled": True})
        # No secrets in this section
        self.assertEqual(response.json()["secrets"], {})

    def test_vector_families_list_rejected(self):
        # families is a dict keyed by family name, not a plain list — must 422
        response = self.client.patch("/api/setup/vector", json={"families": ["tickets"]})
        self.assertEqual(response.status_code, 422)

    def test_status_includes_embeddings_but_missing_unchanged(self):
        body = self.client.get("/api/setup/status").json()

        self.assertIn("embeddings", body["sections"])
        # Vector store is optional — it never blocks "configured"
        self.assertEqual(len(body["missing"]), 4)
        self.assertFalse(any("embed" in m.lower() for m in body["missing"]))

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

        with patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client) as factory:
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

        with patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client) as factory:
            self.client.post("/api/setup/test-itop", json={"user": "ai"})

        self.assertEqual(factory.call_args.args[0].pwd, "stored-pw")

    def test_itop_probe_reports_connection_error(self):
        client = MagicMock()
        client.schema.return_value.find_one = AsyncMock(side_effect=ConnectionError("refused"))
        client.aclose = AsyncMock()

        with patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client):
            body = self.client.post("/api/setup/test-itop", json={"url": "http://itop/rest.php", "token": "tok"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("refused", body["error"])

    def test_itop_probe_no_person_linked(self):
        client = MagicMock()
        client.schema.return_value.find_one = AsyncMock(return_value=None)
        client.aclose = AsyncMock()

        with patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client):
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

    def test_llm_probe_without_api_key_where_the_provider_needs_one(self):
        body = self.client.post("/api/setup/test-llm", json={"provider": "openai", "model": "gpt-test"}).json()

        self.assertFalse(body["ok"])
        # The complaint is about the key, not the base URL openai does not use
        self.assertIn("API key", body["error"])

    def test_llm_probe_success_strips_thinking(self):
        llm = _fake_llm(content="<think>hmm</think>OK")

        with patch("itop_ai_assistant.admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm", json={"base_url": "http://llm/v1", "model": "gpt-test"}
            ).json()

        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["provider"], "openai_compatible")
        self.assertEqual(body["response"], "OK")
        self.assertTrue(body["tool_calling"])

    def test_llm_probe_reports_a_model_that_cannot_call_tools(self):
        llm = _fake_llm(content="OK", tool_calls=[])

        with patch("itop_ai_assistant.admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm", json={"base_url": "http://llm/v1", "model": "gpt-test"}
            ).json()

        # The endpoint answers, but the module cannot use it
        self.assertTrue(body["ok"])
        self.assertFalse(body["tool_calling"])

    def test_llm_probe_does_not_force_tool_choice_unless_the_endpoint_accepts_it(self):
        llm = _fake_llm(content="OK")

        with patch("itop_ai_assistant.admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm", json={"base_url": "http://llm/v1", "model": "gpt-test"}
            ).json()

        self.assertNotIn("forced_tool_choice_ok", body)
        self.assertEqual(llm.bind_kwargs, [{}])

    def test_llm_probe_verifies_a_forced_tool_choice(self):
        llm = _fake_llm(content="OK")

        with patch("itop_ai_assistant.admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm",
                json={"base_url": "http://llm/v1", "model": "gpt-test", "supports_forced_tool_choice": True},
            ).json()

        self.assertTrue(body["forced_tool_choice_ok"])
        self.assertEqual(llm.bind_kwargs, [{"tool_choice": "any"}])

    def test_llm_probe_reports_a_rejected_tool_choice_without_failing_the_endpoint(self):
        # DeepSeek's HTTP 400 on a forced choice: the endpoint is alive, the
        # user's answer about it is wrong
        llm = _fake_llm(content="OK", tool_error=RuntimeError("Thinking mode does not support this tool_choice"))

        with patch("itop_ai_assistant.admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm",
                json={"base_url": "http://llm/v1", "model": "gpt-test", "supports_forced_tool_choice": True},
            ).json()

        self.assertTrue(body["ok"])
        self.assertFalse(body["forced_tool_choice_ok"])
        self.assertIn("Thinking mode", body["tool_error"])

    def test_llm_probe_reports_error(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=TimeoutError("no answer"))

        with patch("itop_ai_assistant.admin.setup.create_llm", return_value=llm):
            body = self.client.post(
                "/api/setup/test-llm", json={"base_url": "http://llm/v1", "model": "gpt-test"}
            ).json()

        self.assertFalse(body["ok"])
        self.assertIn("TimeoutError", body["error"])


class TestEmbeddingsProbe(SetupApiTestCase):
    def test_probe_without_base_url(self):
        body = self.client.post("/api/setup/test-embeddings", json={"model": "bge-m3"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("endpoint", body["error"])

    def test_probe_without_model(self):
        body = self.client.post("/api/setup/test-embeddings", json={"base_url": "http://emb/v1"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("model", body["error"])

    def test_probe_success_reports_measured_dimension(self):
        client = MagicMock()
        client.embed_raw = AsyncMock(return_value=[[0.0] * 768])
        client.aclose = AsyncMock()

        with patch("itop_ai_assistant.vector.use_cases.probe.EmbeddingsClient", return_value=client):
            body = self.client.post(
                "/api/setup/test-embeddings",
                json={"base_url": "http://emb/v1", "model": "bge-m3", "dimension": 1024},
            ).json()

        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "bge-m3")
        self.assertEqual(body["dimension"], 768)
        self.assertFalse(body["dimension_match"])  # config says 1024, endpoint returned 768
        client.aclose.assert_awaited_once()

    def test_probe_dimension_match(self):
        client = MagicMock()
        client.embed_raw = AsyncMock(return_value=[[0.0] * 1024])
        client.aclose = AsyncMock()

        with patch("itop_ai_assistant.vector.use_cases.probe.EmbeddingsClient", return_value=client):
            body = self.client.post(
                "/api/setup/test-embeddings", json={"base_url": "http://emb/v1", "model": "bge-m3"}
            ).json()

        self.assertTrue(body["ok"])
        self.assertTrue(body["dimension_match"])

    def test_probe_reports_error(self):
        client = MagicMock()
        client.embed_raw = AsyncMock(side_effect=TimeoutError("no answer"))
        client.aclose = AsyncMock()

        with patch("itop_ai_assistant.vector.use_cases.probe.EmbeddingsClient", return_value=client):
            body = self.client.post(
                "/api/setup/test-embeddings", json={"base_url": "http://emb/v1", "model": "bge-m3"}
            ).json()

        self.assertFalse(body["ok"])
        self.assertIn("TimeoutError", body["error"])
        client.aclose.assert_awaited_once()

    def test_probe_invalid_body_rejected(self):
        response = self.client.post("/api/setup/test-embeddings", json={"batch_size": "many"})
        self.assertEqual(response.status_code, 422)


class _ProvisioningTransport(httpx.AsyncBaseTransport):
    """Nothing exists yet, every create succeeds; records what was sent."""

    def __init__(self) -> None:
        self.operations: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(parse_qs(request.content.decode())["json_data"][0])
        self.operations.append(payload.get("operation", ""))
        if payload.get("operation") == "core/create":
            return httpx.Response(200, json={"code": 0, "objects": {"x::1": {"key": "1", "fields": {}}}})
        return httpx.Response(200, json={"code": 0, "objects": None})


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
            patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client) as factory,
            patch("itop_ai_assistant.admin.setup.provision_itop", AsyncMock(return_value=report)) as provision,
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

    def test_provisioning_works_while_the_dry_run_is_on(self):
        """Setting iTop up is not a module acting on a ticket (REQ-006 R4).

        The customer switches the dry run on *before* the installation is
        finished, so a ban hung wide enough to cover the wizard would make the
        mode impossible to try. What keeps them apart is the topology: this
        client comes from `create_itop_client`, and the ban is a view handed
        out by `ItopRepositories.for_principal`, which the wizard never calls.
        """
        self.client.patch("/api/setup/security", json={"webhook_token": "wh"})
        self.client.patch("/api/setup/platform", json={"dry_run": True})
        transport = _ProvisioningTransport()
        client = Itop(url="http://itop/rest.php", version="1.3", auth_user="admin", transport=transport)

        with patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client):
            body = self.client.post(
                "/api/setup/provision-itop",
                json={"backend_url": "http://assistant:8000", "user": "admin", "pwd": "admin-pw"},
            ).json()

        self.assertTrue(body["ok"])
        self.assertTrue(any(item["status"] == "created" for item in body["report"]))
        self.assertIn("core/create", transport.operations)

    def test_provision_error_reported(self):
        self.client.patch("/api/setup/security", json={"webhook_token": "wh"})
        client = MagicMock()
        client.aclose = AsyncMock()

        with (
            patch("itop_ai_assistant.admin.setup.create_itop_client", return_value=client),
            patch("itop_ai_assistant.admin.setup.provision_itop", AsyncMock(side_effect=ConnectionError("refused"))),
        ):
            body = self.client.post(
                "/api/setup/provision-itop", json={"backend_url": "http://assistant:8000", "token": "tok"}
            ).json()

        self.assertFalse(body["ok"])
        self.assertIn("refused", body["error"])
        client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
