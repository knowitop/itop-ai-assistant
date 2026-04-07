import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from config import Settings, get_settings

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

    def test_missing_itop_auth_raises(self):
        env = {k: v for k, v in _REQUIRED.items() if k != "ITOP_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)  # disable .env so file-based auth can't satisfy the check


class TestGetSettings(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def test_returns_same_instance(self):
        with patch.dict(os.environ, _REQUIRED, clear=True):
            self.assertIs(get_settings(), get_settings())
