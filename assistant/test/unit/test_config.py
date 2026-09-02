import os
import unittest
from unittest.mock import patch

from pydantic import BaseModel, ValidationError

from itop_ai_assistant.config import (
    EmbeddingsConfig,
    ItopConfig,
    LlmConfig,
    MappingConfig,
    MappingsConfig,
    Settings,
    get_settings,
    missing_setup,
)

_REQUIRED = {
    "LLM_BASE_URL": "http://localhost/v1",
    "LLM_MODEL": "test-model",
    "LLM_API_KEY": "test-key",
    "ITOP_TOKEN": "test-token",
}


class TestSettings(unittest.TestCase):
    def test_env_var_overrides_yaml(self):
        with patch.dict(os.environ, {**_REQUIRED, "LLM_MODEL": "override-model"}, clear=True):
            s = Settings()
        self.assertEqual(s.llm_model, "override-model")

    def test_secret_not_in_str(self):
        with patch.dict(os.environ, _REQUIRED, clear=True):
            s = Settings()
        assert s.llm_api_key is not None
        self.assertNotIn(s.llm_api_key.get_secret_value(), str(s.llm_api_key))

    def test_get_secret_value_returns_actual(self):
        with patch.dict(os.environ, {**_REQUIRED, "LLM_API_KEY": "my-secret-key"}, clear=True):
            s = Settings()
        assert s.llm_api_key is not None
        self.assertEqual(s.llm_api_key.get_secret_value(), "my-secret-key")

    def test_new_fields_have_defaults(self):
        with patch.dict(os.environ, _REQUIRED, clear=True):
            s = Settings(_env_file=None)
        self.assertIsNone(s.webhook_token)
        self.assertEqual(s.itop_api_version, "1.3")
        self.assertEqual(s.itop_timeout, 30.0)
        self.assertEqual(s.state_ttl_days, 30)
        self.assertEqual(s.llm_think_tags, ["think", "thinking", "reasoning"])

    def test_webhook_token_is_secret(self):
        with patch.dict(os.environ, {**_REQUIRED, "WEBHOOK_TOKEN": "hunter2"}, clear=True):
            s = Settings(_env_file=None)
        assert s.webhook_token is not None
        self.assertNotIn("hunter2", str(s.webhook_token))
        self.assertEqual(s.webhook_token.get_secret_value(), "hunter2")

    def test_starts_with_no_configuration_at_all(self):
        # Zero-config start: no field is required anymore — the app must
        # boot with env/yaml defaults alone; setup completeness is checked
        # at runtime (missing_setup), not at startup.
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)  # config.yaml may still supply non-secret defaults
        self.assertIsNone(s.itop_user)
        self.assertIsNone(s.itop_pwd)
        self.assertIsNone(s.itop_token)
        self.assertFalse(s.itop.has_auth)


class TestRuntimeSections(unittest.TestCase):
    def _settings(self, extra: dict[str, str] | None = None) -> Settings:
        with patch.dict(os.environ, {**_REQUIRED, **(extra or {})}, clear=True):
            return Settings(_env_file=None)

    def test_itop_section_defaults_from_flat_env(self):
        s = self._settings({"ITOP_URL": "http://example/rest.php", "ITOP_TOKEN": "tok-123"})
        itop = s.itop
        self.assertEqual(itop.url, "http://example/rest.php")
        self.assertEqual(itop.token, "tok-123")  # plain str for storage round-trip
        self.assertTrue(itop.has_auth)

    def test_llm_section_defaults_from_flat_env(self):
        s = self._settings()
        llm = s.llm
        self.assertEqual(llm.model, "test-model")
        self.assertEqual(llm.api_key, "test-key")
        self.assertEqual(llm.think_tags, ["think", "thinking", "reasoning"])
        # Unset provider keeps the pre-registry behaviour
        self.assertEqual(llm.provider, "openai_compatible")

    def test_llm_provider_and_params_from_flat_env(self):
        s = self._settings(
            {
                "LLM_PROVIDER": "google_genai",
                "LLM_PARAMS": '{"temperature": 0.2}',
                "LLM_SUPPORTS_FORCED_TOOL_CHOICE": "true",
            }
        )
        self.assertEqual(s.llm.provider, "google_genai")
        self.assertEqual(s.llm.params, {"temperature": 0.2})
        self.assertTrue(s.llm.supports_forced_tool_choice)

    def test_blank_params_line_still_boots(self):
        # docker/.env.dist ships these blank; a parse error there would stop
        # the app from starting at all
        s = self._settings({"LLM_PARAMS": "", "LLM_SUPPORTS_FORCED_TOOL_CHOICE": ""})
        self.assertEqual(s.llm.params, {})
        self.assertIsNone(s.llm.supports_forced_tool_choice)

    def test_security_section_defaults_from_flat_env(self):
        s = self._settings({"WEBHOOK_TOKEN": "wh", "ADMIN_TOKEN": "adm"})
        sec = s.security
        self.assertEqual(sec.webhook_token, "wh")
        self.assertEqual(sec.admin_token, "adm")

    def test_secret_fields_declared(self):
        self.assertEqual(ItopConfig.SECRET_FIELDS, frozenset({"pwd", "token"}))
        self.assertEqual(LlmConfig.SECRET_FIELDS, frozenset({"api_key"}))

    def test_blank_env_secret_means_not_set(self):
        # Blank lines in .env (WEBHOOK_TOKEN=) must not enable auth with an
        # empty token or count as iTop credentials
        s = self._settings({"WEBHOOK_TOKEN": "", "ITOP_TOKEN": ""})
        self.assertIsNone(s.security.webhook_token)
        self.assertIsNone(s.itop.token)
        self.assertFalse(s.itop.has_auth)

    def test_has_auth_requires_full_basic_pair(self):
        self.assertFalse(ItopConfig(user="admin").has_auth)
        self.assertTrue(ItopConfig(user="admin", pwd="secret").has_auth)
        self.assertTrue(ItopConfig(token="tok").has_auth)

    def test_embeddings_section_defaults_from_flat_env(self):
        s = self._settings(
            {
                "EMBEDDINGS_BASE_URL": "http://emb/v1",
                "EMBEDDINGS_MODEL": "bge-m3",
                "EMBEDDINGS_API_KEY": "emb-key",
                "EMBEDDINGS_DIMENSION": "768",
            }
        )
        emb = s.embeddings
        self.assertEqual(emb.base_url, "http://emb/v1")
        self.assertEqual(emb.model, "bge-m3")
        self.assertEqual(emb.api_key, "emb-key")  # plain str for storage round-trip
        self.assertEqual(emb.dimension, 768)
        self.assertEqual(emb.batch_size, 32)

    def test_embeddings_secret_fields_and_blank_api_key(self):
        self.assertEqual(EmbeddingsConfig.SECRET_FIELDS, frozenset({"api_key"}))
        self.assertIsNone(EmbeddingsConfig(api_key="").api_key)

    def test_embeddings_dimension_is_bounded_below_only(self):
        self.assertEqual(EmbeddingsConfig(dimension=4096).dimension, 4096)
        with self.assertRaises(ValidationError):
            EmbeddingsConfig(dimension=0)

    def test_embeddings_unconfigured_by_default(self):
        s = self._settings()
        self.assertIsNone(s.embeddings.base_url)
        self.assertIsNone(s.embeddings.model)


class TestQdrantUrl(unittest.TestCase):
    def test_qdrant_url_defaults_to_none(self):
        with patch.dict(os.environ, _REQUIRED, clear=True):
            s = Settings(_env_file=None)
        self.assertIsNone(s.qdrant_url)


class _DummyModuleConfig(BaseModel):
    enabled: bool = False
    rounds: int = 1


class TestModuleDefaults(unittest.TestCase):
    """`Settings` carries no field for a business module — see TASK-024.

    A module's own model is the only thing that knows its shape;
    `module_defaults` is the sole path `config.py` offers back.
    """

    def test_unset_module_returns_model_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
        self.assertEqual(s.module_defaults("nope", _DummyModuleConfig), _DummyModuleConfig())

    def test_module_config_overrides_apply(self):
        # Like every other complex field here (e.g. LLM_PARAMS), env carries
        # it as JSON — settings_customise_sources deliberately excludes
        # constructor kwargs, so this is the real path, not `Settings(...)`.
        env = {"MODULE_CONFIG": '{"dummy": {"enabled": true, "rounds": 5}}'}
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
        cfg = s.module_defaults("dummy", _DummyModuleConfig)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.rounds, 5)

    def test_settings_has_no_attribute_for_a_business_module(self):
        # The whole point: config.py does not know "intake" or any other
        # module name — there is no typed field to accidentally shadow.
        # "vector" belongs here too (TASK-036): not a module, but resolved
        # through the exact same fallback since it has no attribute either.
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
        self.assertFalse(hasattr(s, "intake"))
        self.assertFalse(hasattr(s, "selfcheck"))
        self.assertFalse(hasattr(s, "vector"))


class TestMissingSetup(unittest.TestCase):
    def test_unconfigured_reports_all_steps(self):
        # No url + no auth for iTop, no base_url + no model for LLM.
        missing = missing_setup(ItopConfig(), LlmConfig())
        self.assertEqual(len(missing), 4)
        self.assertTrue(any("iTop" in m for m in missing))
        self.assertTrue(any("LLM" in m for m in missing))

    def test_url_required_even_with_auth(self):
        missing = missing_setup(ItopConfig(token="tok"), LlmConfig(base_url="http://x/v1", model="m"))
        self.assertEqual(missing, ["iTop REST API URL (itop: url)"])

    def test_base_url_required_even_with_model(self):
        missing = missing_setup(ItopConfig(url="http://x", token="tok"), LlmConfig(model="m"))
        self.assertEqual(missing, ["LLM endpoint (llm: base_url)"])

    def test_fully_configured_is_empty(self):
        itop = ItopConfig(url="http://itop/rest.php", token="tok")
        llm = LlmConfig(base_url="http://llm/v1", model="m")
        self.assertEqual(missing_setup(itop, llm), [])

    def test_required_llm_fields_follow_the_provider(self):
        itop = ItopConfig(url="http://x", token="tok")
        # Gemini has no endpoint to configure but does need a key
        self.assertEqual(
            missing_setup(itop, LlmConfig(provider="google_genai", model="m")),
            ["LLM API key (llm: api_key)"],
        )
        self.assertEqual(missing_setup(itop, LlmConfig(provider="google_genai", model="m", api_key="k")), [])
        # Ollama needs neither: the base URL has a working default
        self.assertEqual(missing_setup(itop, LlmConfig(provider="ollama", model="m")), [])


class TestLlmSection(unittest.TestCase):
    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            LlmConfig(provider="gpt5-please")

        self.assertIn("openai_compatible", str(ctx.exception))

    def test_params_may_not_shadow_the_sections_own_fields(self):
        # Otherwise a stray "model" in params would quietly beat llm.model
        with self.assertRaises(ValidationError):
            LlmConfig(params={"model": "something-else"})

    def test_forced_tool_choice_falls_back_to_the_provider(self):
        self.assertFalse(LlmConfig(provider="openai_compatible").endpoint_forces_tool_choice)
        self.assertTrue(LlmConfig(provider="openai").endpoint_forces_tool_choice)
        self.assertFalse(LlmConfig(provider="ollama").endpoint_forces_tool_choice)

    def test_an_explicit_answer_wins(self):
        # The deployment owner knows what sits behind their URL
        self.assertTrue(
            LlmConfig(provider="openai_compatible", supports_forced_tool_choice=True).endpoint_forces_tool_choice
        )
        self.assertFalse(LlmConfig(provider="openai", supports_forced_tool_choice=False).endpoint_forces_tool_choice)


class TestMappings(unittest.TestCase):
    """The section holds what a deployment changed; resolving it against the
    family declaration is the repository's job (`test_object_repository.py`)."""

    def test_a_stock_deployment_overrides_nothing_but_the_incident_class(self):
        mappings = MappingsConfig()

        self.assertEqual({}, mappings.for_family("tickets").fields)
        self.assertEqual({"Incident": {"request_type": None}}, mappings.for_family("tickets").class_overrides)

    def test_a_family_the_section_says_nothing_about_maps_as_declared(self):
        self.assertEqual({}, MappingsConfig(families={}).for_family("faq").fields)

    def test_a_name_that_is_not_a_field_of_the_family_is_refused(self):
        with self.assertRaises(ValidationError):
            MappingsConfig(families={"tickets": MappingConfig(fields={"no_such_field": "x"})})
        with self.assertRaises(ValidationError):
            MappingsConfig(families={"tickets": MappingConfig(class_overrides={"Incident": {"nope": None}})})

    def test_a_family_nothing_declares_is_inert_rather_than_fatal(self):
        # Refusing would take the whole section down with it on start
        # (ADR-026), and the entry configures nothing either way.
        with self.assertLogs("itop_ai_assistant.config", level="WARNING"):
            mappings = MappingsConfig(families={"kb_articles": MappingConfig()})

        self.assertIn("kb_articles", mappings.families)


class TestGetSettings(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def test_returns_same_instance(self):
        with patch.dict(os.environ, _REQUIRED, clear=True):
            self.assertIs(get_settings(), get_settings())
