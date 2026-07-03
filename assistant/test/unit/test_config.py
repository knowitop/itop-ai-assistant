import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from config import ItopConfig, LlmConfig, Settings, TicketMappingConfig, get_settings, missing_setup

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
        self.assertEqual(s.enrichment.max_rounds, 2)
        self.assertEqual(s.enrichment.max_classify_rounds, 2)
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


class TestTicketMapping(unittest.TestCase):
    def test_default_mapping(self):
        mapping = TicketMappingConfig()
        resolved = mapping.for_class("UserRequest")
        self.assertEqual(resolved["subcategory_id"], "servicesubcategory_id")
        self.assertEqual(resolved["caller_name"], "caller_id_friendlyname")
        self.assertEqual(resolved["request_type"], "request_type")

    def test_incident_override_drops_request_type(self):
        mapping = TicketMappingConfig()
        resolved = mapping.for_class("Incident")
        self.assertIsNone(resolved["request_type"])
        self.assertEqual(resolved["title"], "title")

    def test_partial_fields_override_keeps_defaults(self):
        mapping = TicketMappingConfig(fields={"title": "custom_title"})
        resolved = mapping.for_class("UserRequest")
        self.assertEqual(resolved["title"], "custom_title")
        self.assertEqual(resolved["description"], "description")

    def test_unknown_override_field_raises(self):
        with self.assertRaises(ValidationError):
            TicketMappingConfig(class_overrides={"Incident": {"no_such_field": None}})

    def test_default_active_statuses(self):
        self.assertEqual(TicketMappingConfig().active_statuses, ["new"])


class TestGetSettings(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def test_returns_same_instance(self):
        with patch.dict(os.environ, _REQUIRED, clear=True):
            self.assertIs(get_settings(), get_settings())
