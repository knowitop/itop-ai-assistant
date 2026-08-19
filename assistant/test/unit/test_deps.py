import unittest
from unittest.mock import AsyncMock, patch

from itop_ai_assistant.config import LlmConfig, get_settings
from itop_ai_assistant.core.deps import build_deps, create_llm
from itop_ai_assistant.pipelines.registry import build_registry


class TestAclose(unittest.IsolatedAsyncioTestCase):
    async def test_aclose_closes_the_connection_not_the_repository_factory(self):
        """Ownership of the pool stays at the composition root: the factory that
        builds repository sets has no lifecycle method to reach for."""
        settings = get_settings()
        deps = build_deps(settings, build_registry(settings))

        with patch.object(deps.itop_connection, "aclose", new_callable=AsyncMock) as closed:
            await deps.aclose()

        closed.assert_awaited_once()
        self.assertFalse(hasattr(deps.itop, "aclose"))


class TestVectorStore(unittest.TestCase):
    def test_vector_store_is_unconfigured_without_a_url(self):
        settings = get_settings().model_copy(update={"qdrant_url": None})
        deps = build_deps(settings, build_registry(settings))

        self.assertFalse(deps.vector.vector_store.configured)

    def test_vector_store_is_configured_from_qdrant_url(self):
        settings = get_settings().model_copy(update={"qdrant_url": "http://qdrant:6333"})
        deps = build_deps(settings, build_registry(settings))

        self.assertTrue(deps.vector.vector_store.configured)


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
