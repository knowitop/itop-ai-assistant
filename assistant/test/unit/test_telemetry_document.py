"""Assembling the day's document: shape, sources, and what cannot get in."""

import unittest
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import fakeredis
from pydantic import ValidationError

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.config import LlmConfig, Settings, get_settings
from itop_ai_assistant.pipelines.registry import ModuleInfo
from itop_ai_assistant.settings.config_store import RedisConfigStore
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.state.install import InstallIdentity
from itop_ai_assistant.telemetry.builder import DocumentBuilder
from itop_ai_assistant.telemetry.document import TelemetryDocument

DAY = date(2026, 8, 20)


def _registry(*modules: ModuleInfo) -> MagicMock:
    return MagicMock(modules=list(modules))


class DocumentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # `Settings` drops `init_settings` (see `settings_customise_sources`),
        # so a value a test cares about is set on the instance or through the
        # config store, never handed to the constructor.
        self.settings = Settings()
        self.settings.qdrant_url = None
        self.config_store = RedisConfigStore(self.redis, self.settings)
        self.counters = DailyCounters(self.redis)
        self.install = InstallIdentity(self.redis)
        self.vector_search = AsyncMock(available=AsyncMock(return_value=False))

    def _builder(self, *modules: ModuleInfo) -> DocumentBuilder:
        return DocumentBuilder(
            self.settings,
            self.config_store,
            _registry(*modules),
            self.counters,
            self.install,
            self.vector_search,
        )


class TestShape(DocumentTestCase):
    async def test_a_fresh_installation_still_produces_a_whole_document(self):
        await self.config_store.set("llm", {"provider": "openai", "model": "gpt-4o"}, LlmConfig)

        document = await self._builder().build(DAY)

        self.assertTrue(document.install_id)
        self.assertEqual(DAY, document.day)
        self.assertEqual("openai", document.configuration.llm_provider)
        self.assertEqual("gpt-4o", document.configuration.llm_model)
        self.assertFalse(document.configuration.dry_run)

    async def test_a_model_nobody_configured_is_empty_not_invented(self):
        document = await self._builder().build(DAY)

        self.assertIsNone(document.configuration.llm_model)

    async def test_every_counter_is_present_even_at_zero(self):
        """R3's activity group must have a stable shape — a reader comparing
        two days must not have to ask whether a missing key means zero."""
        document = await self._builder().build(DAY)

        self.assertEqual({counter.value for counter in Counter}, set(document.activity))
        self.assertEqual({0}, set(document.activity.values()))

    async def test_the_activity_is_the_day_that_was_asked_for(self):
        await self.counters.bump(Counter.RUNS_WEBHOOK, 3)
        today = datetime.now(UTC).date()

        self.assertEqual(3, (await self._builder().build(today)).activity[Counter.RUNS_WEBHOOK.value])
        self.assertEqual(0, (await self._builder().build(DAY)).activity[Counter.RUNS_WEBHOOK.value])

    async def test_the_language_is_empty_until_somebody_opens_the_admin_ui(self):
        self.assertIsNone((await self._builder().build(DAY)).environment.admin_language)

        await self.install.remember_language("ru-RU")

        self.assertEqual("ru", (await self._builder().build(DAY)).environment.admin_language)

    async def test_the_vector_layer_is_reported_as_answering_not_as_deployed(self):
        """A deployed Qdrant and a working layer are two different answers —
        the requirement asks whether the layer earns its complexity."""
        self.settings.qdrant_url = "http://qdrant:6333"
        self.vector_search.available.return_value = False

        document = await self._builder().build(DAY)

        self.assertTrue(document.environment.qdrant)
        self.assertFalse(document.environment.vector_available)


class TestConfigurationGroup(DocumentTestCase):
    """The half of R4 the type system enforces (`builder._module_settings`)."""

    def _intake(self) -> ModuleInfo:
        return ModuleInfo(name="intake", description="", config_model=IntakeConfig)

    async def test_a_modules_numbers_and_switches_travel(self):
        settings = (await self._builder(self._intake()).build(DAY)).configuration.settings

        self.assertIs(True, settings["intake_enabled"])
        self.assertEqual(3, settings["intake_max_questions"])
        self.assertEqual(9, settings["intake_max_iterations"])

    async def test_nothing_a_module_wrote_as_text_can_travel(self):
        """Not "we left them out" — they are neither a `bool` nor an `int`,
        and that is the whole guarantee."""
        settings = (await self._builder(self._intake()).build(DAY)).configuration.settings

        for field in ("classes", "active_statuses", "classify_service_oql", "handoff_fallback_note", "model"):
            with self.subTest(field=field):
                self.assertNotIn(f"intake_{field}", settings)

    async def test_a_float_is_not_an_integer(self):
        settings = (await self._builder(self._intake()).build(DAY)).configuration.settings

        self.assertNotIn("intake_similar_min_score", settings)

    async def test_the_vector_section_travels_without_being_a_module(self):
        settings = (await self._builder().build(DAY)).configuration.settings

        self.assertIs(False, settings["vector_enabled"])
        self.assertEqual(300, settings["vector_sweep_interval_seconds"])
        self.assertNotIn("vector_families", settings)

    async def test_an_edited_section_is_what_travels(self):
        await self.config_store.set("intake", {"max_questions": 7}, IntakeConfig)

        settings = (await self._builder(self._intake()).build(DAY)).configuration.settings

        self.assertEqual(7, settings["intake_max_questions"])

    async def test_a_module_without_config_contributes_nothing(self):
        module = ModuleInfo(name="selfcheck", description="", config_model=None)

        settings = (await self._builder(module).build(DAY)).configuration.settings

        self.assertEqual([], [key for key in settings if key.startswith("selfcheck_")])


class TestTheDocumentRefusesToBeWidened(unittest.TestCase):
    """The sender and the preview must see one shape, and neither may add to
    it — a preview is only honest if it renders what the sender sends."""

    def _document(self, **overrides) -> dict:
        base = {
            "install_id": "x",
            "day": DAY,
            "build": {"version": "0.1", "commit": None, "python_version": "3.13", "containerized": False},
            "environment": {
                "qdrant": False,
                "vector_available": False,
                "admin_language": None,
                "utc_offset_minutes": 0,
            },
            "configuration": {
                "dry_run": False,
                "llm_provider": None,
                "llm_model": None,
                "settings": {},
            },
            "activity": {counter.value: 0 for counter in Counter},
        }
        return {**base, **overrides}

    def test_an_extra_field_is_refused(self):
        with self.assertRaises(ValidationError):
            TelemetryDocument(**self._document(ticket_title="Printer is on fire"))

    def test_a_string_cannot_coerce_into_a_counter(self):
        activity = {counter.value: 0 for counter in Counter}
        activity[Counter.RUNS_WEBHOOK.value] = "12"

        with self.assertRaises(ValidationError):
            TelemetryDocument(**self._document(activity=activity))

    def test_a_string_cannot_coerce_into_a_module_setting(self):
        configuration = self._document()["configuration"] | {"settings": {"intake_note": "call Vasily"}}

        with self.assertRaises(ValidationError):
            TelemetryDocument(**self._document(configuration=configuration))

    def test_a_missing_counter_is_refused(self):
        activity = {counter.value: 0 for counter in Counter}
        activity.pop(Counter.LLM_CALLS.value)

        with self.assertRaises(ValidationError):
            TelemetryDocument(**self._document(activity=activity))

    def test_a_key_that_is_not_a_counter_is_refused(self):
        activity = {counter.value: 0 for counter in Counter} | {"tickets_by_caller": 1}

        with self.assertRaises(ValidationError):
            TelemetryDocument(**self._document(activity=activity))
