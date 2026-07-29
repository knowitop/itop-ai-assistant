"""Invariants of the provider registry.

The registry is data, so what is worth testing is the contract the rest of the
code reads out of it — not the individual values.
"""

import unittest

from llm_providers import DEFAULT_PROVIDER, PROVIDERS, get_provider

_FIELD_MODES = {"required", "optional", "unused"}


class TestRegistry(unittest.TestCase):
    def test_entries_are_keyed_by_their_own_id(self):
        for key, provider in PROVIDERS.items():
            self.assertEqual(key, provider.id)

    def test_every_entry_is_usable(self):
        for provider in PROVIDERS.values():
            with self.subTest(provider=provider.id):
                self.assertTrue(provider.langchain_provider)
                self.assertTrue(provider.label)
                self.assertIn(provider.base_url_mode, _FIELD_MODES)
                self.assertIn(provider.api_key_mode, _FIELD_MODES)

    def test_a_base_url_that_matters_has_an_example(self):
        for provider in PROVIDERS.values():
            with self.subTest(provider=provider.id):
                if provider.base_url_mode == "unused":
                    self.assertIsNone(provider.base_url_placeholder)
                else:
                    self.assertTrue(provider.base_url_placeholder)

    def test_the_default_provider_exists(self):
        self.assertIn(DEFAULT_PROVIDER, PROVIDERS)

    def test_unknown_provider_names_the_known_ones(self):
        with self.assertRaises(ValueError) as ctx:
            get_provider("gpt5-please")

        self.assertIn("openai_compatible", str(ctx.exception))


class TestForcedToolChoiceKnowledge(unittest.TestCase):
    def test_unknown_only_where_the_url_hides_the_server(self):
        """`None` is a question for the user, not a shrug.

        It is allowed exactly where the provider cannot know the answer:
        behind an OpenAI-compatible URL sit both vLLM (accepts a forced
        tool_choice) and DeepSeek (HTTP 400).
        """
        unknown = {p.id for p in PROVIDERS.values() if p.supports_forced_tool_choice is None}

        self.assertEqual(unknown, {"openai_compatible"})
