import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from itop_ai_assistant.config import ItopConfig, LlmConfig, TicketMappingConfig, get_settings
from itop_ai_assistant.deps import ItopProvider, build_deps, create_llm
from itop_ai_assistant.principal import Principal

_ENGINEER = Principal.delegated("engineer-token", login="jdoe", name="John Doe")


class _FakeConfigStore:
    def __init__(self):
        self.sections = {
            "itop": ItopConfig(url="http://one/rest.php", token="tok"),
            "ticket_mapping": TicketMappingConfig(),
        }

    async def get(self, module, model):
        return self.sections[module]


class TestItopProvider(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = _FakeConfigStore()
        self.provider = ItopProvider(self.store)

    async def test_same_config_returns_same_bundle(self):
        b1 = await self.provider.get()
        b2 = await self.provider.get()
        self.assertIs(b1, b2)
        self.assertIs(b1.ticket_repo, b2.ticket_repo)
        self.assertIs(b1.access_repo, b2.access_repo)
        await self.provider.aclose()

    async def test_config_change_rebuilds_and_closes_old_client(self):
        b1 = await self.provider.get()
        with patch.object(b1.client, "aclose", new_callable=AsyncMock) as old_close:
            self.store.sections["itop"] = ItopConfig(url="http://two/rest.php", token="tok")
            b2 = await self.provider.get()

        self.assertIsNot(b1, b2)
        old_close.assert_awaited_once()
        await self.provider.aclose()

    async def test_mapping_change_rebuilds_repositories(self):
        b1 = await self.provider.get()
        self.store.sections["ticket_mapping"] = TicketMappingConfig(active_statuses=["new", "assigned"])

        b2 = await self.provider.get()

        self.assertIsNot(b1.ticket_repo, b2.ticket_repo)
        self.assertEqual(b2.ticket_repo.mapping.active_statuses, ["new", "assigned"])
        await self.provider.aclose()

    async def test_aclose_resets_cache(self):
        b1 = await self.provider.get()
        await self.provider.aclose()
        b2 = await self.provider.get()
        self.assertIsNot(b1, b2)
        await self.provider.aclose()


class TestForPrincipal(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = _FakeConfigStore()
        self.provider = ItopProvider(self.store)

    async def asyncTearDown(self):
        await self.provider.aclose()

    async def test_a_delegated_run_talks_to_itop_as_the_engineer(self):
        bundle = await self.provider.for_principal(_ENGINEER, comment="run 42")

        self.assertEqual(bundle.client.auth.token, "engineer-token")
        self.assertEqual(bundle.client.comment, "run 42")

    async def test_the_connection_pool_is_shared_not_duplicated(self):
        base = await self.provider.get()

        bundle = await self.provider.for_principal(_ENGINEER, comment="run 42")

        self.assertIsNot(bundle.client, base.client)
        self.assertIs(bundle.client._http, base.client._http)
        self.assertIs(bundle.ticket_repo.mapping, base.ticket_repo.mapping)

    async def test_the_service_account_keeps_the_connections_own_credentials(self):
        base = await self.provider.get()

        bundle = await self.provider.for_principal(Principal.service(), comment="run 42")

        self.assertEqual(bundle.client.auth, base.client.auth)
        self.assertEqual(bundle.client.comment, "run 42")

    async def test_repositories_cannot_reach_past_their_principal(self):
        bundle = await self.provider.for_principal(_ENGINEER, comment="run 42")

        self.assertIs(bundle.ticket_repo._itop, bundle.client)
        self.assertIs(bundle.catalog_repo._itop, bundle.client)
        self.assertIs(bundle.access_repo._itop, bundle.client)


class TestAiPersonName(unittest.IsolatedAsyncioTestCase):
    """The service account's own name — a property of the connection."""

    def setUp(self):
        self.store = _FakeConfigStore()
        self.provider = ItopProvider(self.store)

    async def asyncTearDown(self):
        await self.provider.aclose()

    def _answer(self, name="ai-assistant"):
        schema = MagicMock()
        schema.find_one = AsyncMock(return_value={"friendlyname": name})
        return schema

    async def test_resolved_once_and_cached(self):
        bundle = await self.provider.get()
        schema = self._answer()
        with patch.object(bundle.client, "schema", return_value=schema):
            self.assertEqual(await self.provider.ai_person_name(), "ai-assistant")
            self.assertEqual(await self.provider.ai_person_name(), "ai-assistant")

        schema.find_one.assert_awaited_once()

    async def test_dropped_when_the_connection_is_rebuilt(self):
        bundle = await self.provider.get()
        with patch.object(bundle.client, "schema", return_value=self._answer("old")):
            await self.provider.ai_person_name()

        self.store.sections["itop"] = ItopConfig(url="http://two/rest.php", token="tok")
        rebuilt = await self.provider.get()
        with patch.object(rebuilt.client, "schema", return_value=self._answer("new")):
            self.assertEqual(await self.provider.ai_person_name(), "new")

    async def test_resolved_as_the_service_account_even_during_a_delegated_run(self):
        """The name answers "is this last comment our own" — the loop guard. It has
        to mean the service account whoever the run acts as, or an engineer's own
        comments would start reading as ours."""
        base = await self.provider.get()
        delegated = await self.provider.for_principal(_ENGINEER, comment="run 42")
        service_schema = self._answer()
        with (
            patch.object(base.client, "schema", return_value=service_schema) as as_service,
            patch.object(delegated.client, "schema") as as_engineer,
        ):
            await self.provider.ai_person_name()

        as_service.assert_called_once_with("Person")
        as_engineer.assert_not_called()

    async def test_a_service_account_without_a_person_is_an_error(self):
        bundle = await self.provider.get()
        schema = MagicMock()
        schema.find_one = AsyncMock(return_value=None)
        with patch.object(bundle.client, "schema", return_value=schema), self.assertRaises(ValueError):
            await self.provider.ai_person_name()


class TestVectorStore(unittest.TestCase):
    def test_vector_store_is_unconfigured_without_a_url(self):
        settings = get_settings().model_copy(update={"qdrant_url": None})
        deps = build_deps(settings)

        self.assertFalse(deps.vector_store.configured)

    def test_vector_store_is_configured_from_qdrant_url(self):
        settings = get_settings().model_copy(update={"qdrant_url": "http://qdrant:6333"})
        deps = build_deps(settings)

        self.assertTrue(deps.vector_store.configured)


class TestCreateLlm(unittest.TestCase):
    def test_model_override(self):
        llm = create_llm(LlmConfig(model="default-model", api_key="k"), model="special")
        self.assertEqual(llm.model_name, "special")

    def test_defaults_from_config(self):
        llm = create_llm(LlmConfig(base_url="http://llm/v1", model="default-model", api_key="k"))
        self.assertEqual(llm.model_name, "default-model")
        self.assertEqual(llm.openai_api_base, "http://llm/v1")

    def test_missing_api_key_gets_placeholder(self):
        # Local endpoints (LM Studio) ignore the key, but ChatOpenAI needs one
        llm = create_llm(LlmConfig(model="m"))
        self.assertIsNotNone(llm.openai_api_key)

    def test_each_provider_builds_its_own_client(self):
        cases = {
            "openai_compatible": (dict(base_url="http://llm/v1"), "ChatOpenAI"),
            "openai": (dict(api_key="k"), "ChatOpenAI"),
            "google_genai": (dict(api_key="k"), "ChatGoogleGenerativeAI"),
            "ollama": (dict(base_url="http://localhost:11434"), "ChatOllama"),
        }
        for provider, (fields, expected) in cases.items():
            with self.subTest(provider=provider):
                llm = create_llm(LlmConfig(provider=provider, model="m", **fields))
                self.assertEqual(type(llm).__name__, expected)

    def test_base_url_is_not_sent_to_a_provider_that_has_none(self):
        # A stale base_url left over from another provider must not leak into
        # the client and silently redirect the requests
        llm = create_llm(LlmConfig(provider="openai", model="m", api_key="k", base_url="http://stale/v1"))
        self.assertIsNone(llm.openai_api_base)

    def test_the_key_reaches_a_native_provider(self):
        llm = create_llm(LlmConfig(provider="google_genai", model="gemini-2.5-flash", api_key="secret"))
        self.assertEqual(llm.google_api_key.get_secret_value(), "secret")

    def test_params_are_forwarded(self):
        llm = create_llm(LlmConfig(model="m", base_url="http://llm/v1", params={"temperature": 0.4}))
        self.assertEqual(llm.temperature, 0.4)


if __name__ == "__main__":
    unittest.main()
