import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from itop_ai_assistant.settings.module_locales import EMPTY, load_translation, normalize_language

SCHEMA = {
    "properties": {
        "max_questions": {
            "type": "integer",
            "title": "Questions to the requester",
            "description": "In total.",
        },
        "classify_enabled": {"type": "boolean", "title": "Enabled", "x-group": "Classification"},
    }
}


class TestNormalizeLanguage(unittest.TestCase):
    def test_region_is_dropped(self):
        self.assertEqual(normalize_language("ru-RU"), "ru")

    def test_missing_or_malformed_is_no_language(self):
        for value in (None, "", "русский", "en_US", "toolongtag"):
            self.assertIsNone(normalize_language(value), value)

    def test_a_path_is_not_a_language(self):
        """The tag names a file, and it arrives from a query string."""
        for value in ("../../etc/passwd", "ru/../../secrets", "."):
            self.assertIsNone(normalize_language(value), value)


class TranslationTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(TemporaryDirectory()))

    def write(self, lang: str, payload: dict | str) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.dir / f"{lang}.json").write_text(text, encoding="utf-8")


class TestLoading(TranslationTestCase):
    def test_no_directory_no_file_no_language(self):
        self.write("ru", {"description": "Приём"})

        self.assertIs(load_translation(None, "ru"), EMPTY)
        self.assertIs(load_translation(self.dir, "de"), EMPTY)
        self.assertIs(load_translation(self.dir, None), EMPTY)

    def test_broken_file_falls_back_to_english(self):
        """A translation is never worth taking the module's settings away."""
        self.write("ru", "{not json")

        with self.assertLogs("itop_ai_assistant.settings.module_locales", "WARNING"):
            self.assertIs(load_translation(self.dir, "ru"), EMPTY)

    def test_unknown_section_is_reported_and_ignored(self):
        self.write("ru", {"description": "Приём", "labels": {"a": "b"}})

        with self.assertLogs("itop_ai_assistant.settings.module_locales", "WARNING") as logs:
            texts = load_translation(self.dir, "ru")

        self.assertIn("labels", logs.output[0])
        self.assertEqual(texts.module_description("Intake"), "Приём")


class TestApplyingToSchema(TranslationTestCase):
    def test_translates_texts_and_group_heading(self):
        self.write(
            "ru",
            {
                "groups": {"Classification": "Классификация"},
                "fields": {"max_questions": {"title": "Вопросов заявителю", "description": "Всего."}},
            },
        )

        props = load_translation(self.dir, "ru-RU").config_schema(SCHEMA)["properties"]

        self.assertEqual(props["max_questions"]["title"], "Вопросов заявителю")
        self.assertEqual(props["max_questions"]["description"], "Всего.")
        self.assertEqual(props["classify_enabled"]["x-group"], "Классификация")

    def test_untranslated_field_keeps_its_english_and_its_hints(self):
        self.write("ru", {"fields": {"max_questions": {"title": "Вопросов заявителю"}}})

        props = load_translation(self.dir, "ru").config_schema(SCHEMA)["properties"]

        self.assertEqual(props["max_questions"]["description"], "In total.")
        self.assertEqual(props["classify_enabled"]["title"], "Enabled")
        self.assertEqual(props["classify_enabled"]["x-group"], "Classification")

    def test_the_schema_it_was_given_is_not_modified(self):
        """The schema is rebuilt per request from the model; translating must not accumulate."""
        self.write("ru", {"fields": {"max_questions": {"title": "Вопросов заявителю"}}})

        load_translation(self.dir, "ru").config_schema(SCHEMA)

        self.assertEqual(SCHEMA["properties"]["max_questions"]["title"], "Questions to the requester")

    def test_summaries_fall_back_to_the_declaration(self):
        self.write("ru", {"actions": {"process": {"summary": "Обработать заявку"}}})

        texts = load_translation(self.dir, "ru")

        self.assertEqual(texts.action_summary("process", "Process a ticket"), "Обработать заявку")
        self.assertEqual(texts.action_summary("other", "Something else"), "Something else")
        self.assertEqual(texts.schedule_summary("tick", "On a timer"), "On a timer")
