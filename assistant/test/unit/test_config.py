import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from config import Settings, TicketMappingConfig, get_settings

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
        self.assertNotIn(s.llm_api_key.get_secret_value(), str(s.llm_api_key))

    def test_get_secret_value_returns_actual(self):
        with patch.dict(os.environ, {**_REQUIRED, "LLM_API_KEY": "my-secret-key"}, clear=True):
            s = Settings()
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

    def test_webhook_token_is_secret(self):
        with patch.dict(os.environ, {**_REQUIRED, "WEBHOOK_TOKEN": "hunter2"}, clear=True):
            s = Settings(_env_file=None)
        assert s.webhook_token is not None
        self.assertNotIn("hunter2", str(s.webhook_token))
        self.assertEqual(s.webhook_token.get_secret_value(), "hunter2")

    def test_missing_itop_auth_raises(self):
        env = {k: v for k, v in _REQUIRED.items() if k != "ITOP_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)  # disable .env so file-based auth can't satisfy the check


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
