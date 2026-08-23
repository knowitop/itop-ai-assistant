"""The R4 contract, checked where it is enforced.

REQ-009 lists this as a readiness criterion in as many words: no field of the
document may accept an arbitrary string from the configuration, proven by
substituting an unknown value into every enumeration. The hostile inputs are
one list shared by both guards — each is something a real customer's
configuration could plausibly hold, and none of them may come out the other
side.
"""

import unittest

from itop_ai_assistant.core.llm_providers import PROVIDERS
from itop_ai_assistant.telemetry.normalize import (
    OTHER,
    in_container,
    llm_provider,
    model_name,
    python_version,
    utc_offset_minutes,
)

#: Values that must never reach the document as themselves.
HOSTILE = (
    # The example R4 spells out: a model name somebody renamed after himself
    "qwen3-32b-финальный-от-Пети",
    "Внутренняя модель поддержки ООО «Ромашка»",
    "http://llm.internal.acme.corp:8000/v1",
    "gpt-4o but only for the night shift",
    "модель",
    "a" * 200,
    "\n",
    "  ",
)


class TestLlmProvider(unittest.TestCase):
    """A real enumeration: the registry lists every endpoint this build ships."""

    def test_every_shipped_provider_travels_as_itself(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(provider, llm_provider(provider))

    def test_anything_not_in_the_registry_becomes_other(self):
        for value in (*HOSTILE, "anthropic", "openai_compatible_v2", "OPENAI"):
            with self.subTest(value=value):
                self.assertEqual(OTHER, llm_provider(value))

    def test_unset_is_not_the_same_as_unknown(self):
        """A fresh installation has no provider yet — reporting it as `other`
        would hide it among the misconfigured ones."""
        self.assertIsNone(llm_provider(None))
        self.assertIsNone(llm_provider(""))


class TestModelName(unittest.TestCase):
    """Not an enumeration — a shape. Anything shaped like a model id travels."""

    def test_model_identifiers_travel_as_themselves(self):
        for value in ("qwen3-32b", "gpt-4o-mini", "deepseek-chat", "llama3.2:3b", "gemini-2.5-flash"):
            with self.subTest(value=value):
                self.assertEqual(value, model_name(value))

    def test_a_registry_prefix_is_still_a_model_id(self):
        self.assertEqual("qwen/qwen3-32b", model_name("Qwen/Qwen3-32B"))

    def test_anything_not_shaped_like_one_becomes_other(self):
        for value in HOSTILE:
            with self.subTest(value=value):
                self.assertEqual(OTHER, model_name(value))

    def test_unset_is_not_the_same_as_unknown(self):
        self.assertIsNone(model_name(None))
        self.assertIsNone(model_name(""))

    def test_what_the_shape_cannot_catch(self):
        """The limit of guarding by form instead of by enumeration, kept
        visible in the suite rather than only in the task's plan.

        An opaque token and a bare `host:port` are spelled with the same
        characters a model id is. Both mean the model field holds something
        that is not a model — an installation where every call fails — but no
        rule short of a curated list of families separates them, and that list
        was declined on purpose: it would exist for telemetry alone and need
        an entry for every model released anywhere.
        """
        self.assertEqual("sk-proj-ab12cd34ef56gh78", model_name("sk-proj-Ab12Cd34Ef56Gh78"))
        self.assertEqual("llm.internal.acme.corp:8000", model_name("llm.internal.acme.corp:8000"))


class TestFactsAboutTheProcess(unittest.TestCase):
    def test_python_version_carries_no_patch_level(self):
        """A patch level answers nothing the requirement asks and splits every
        installation into a series of near-identical ones."""
        self.assertRegex(python_version(), r"^\d+\.\d+$")

    def test_utc_offset_is_a_whole_number_of_minutes(self):
        offset = utc_offset_minutes()

        self.assertIsInstance(offset, int)
        self.assertGreaterEqual(offset, -24 * 60)
        self.assertLessEqual(offset, 24 * 60)

    def test_container_detection_answers_a_boolean(self):
        self.assertIsInstance(in_container(), bool)
