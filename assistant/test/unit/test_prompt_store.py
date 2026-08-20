import logging
import tempfile
import unittest
from pathlib import Path

import fakeredis.aioredis
from pydantic import BaseModel

from itop_ai_assistant.agents.intake.prompts import PROMPT_VARIABLES, build_intake_prompts
from itop_ai_assistant.agents.intake.prompts import PROMPTS_DIR as INTAKE_PROMPTS_DIR
from itop_ai_assistant.config import get_settings
from itop_ai_assistant.main import check_module_prompts
from itop_ai_assistant.pipelines.registry import ModuleInfo, build_registry
from itop_ai_assistant.settings.prompt_store import (
    FilePromptStore,
    PromptOrigin,
    PromptStoreError,
    RedisPromptStore,
    read_prompt_dir,
)
from itop_ai_assistant.settings.prompt_validation import PromptValidationError, build_prompts

_DEFAULTS_DIRS = {"intake": INTAKE_PROMPTS_DIR}


def _default_prompts() -> dict[str, str]:
    return read_prompt_dir(INTAKE_PROMPTS_DIR)


class TestFilePromptStore(unittest.IsolatedAsyncioTestCase):
    async def test_loads_packaged_defaults(self):
        store = FilePromptStore(_DEFAULTS_DIRS)
        prompts = await store.get("intake")
        self.assertEqual(prompts.effective.keys(), PROMPT_VARIABLES.keys())
        self.assertEqual(set(prompts.origins.values()), {PromptOrigin.DEFAULT})

    async def test_missing_module_raises(self):
        store = FilePromptStore(_DEFAULTS_DIRS)
        with self.assertRaises(PromptStoreError):
            await store.get("no_such_module")

    async def test_empty_defaults_dir_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = Path(tmp) / "empty"
            empty_dir.mkdir()

            store = FilePromptStore({"empty": empty_dir})
            with self.assertRaises(PromptStoreError) as ctx:
                await store.get("empty")

        self.assertIn(str(empty_dir), str(ctx.exception))

    async def test_override_shadows_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_dir = Path(tmp) / "intake"
            override_dir.mkdir()
            (override_dir / "system.md").write_text("Custom system prompt", encoding="utf-8")

            store = FilePromptStore(_DEFAULTS_DIRS, Path(tmp))
            prompts = await store.get("intake")

        self.assertEqual(prompts.effective["system"], "Custom system prompt")
        self.assertEqual(prompts.origins["system"], PromptOrigin.FILE)
        # Non-overridden prompts keep their defaults, and stay ours
        self.assertEqual(prompts.effective["ticket_human"], _default_prompts()["ticket_human"])
        self.assertEqual(prompts.origins["ticket_human"], PromptOrigin.DEFAULT)

    async def test_unknown_override_name_is_ignored_not_fatal(self):
        # A typo in a filename used to fail the boot, taking away the admin UI
        # that reports it (REQ-005)
        with tempfile.TemporaryDirectory() as tmp:
            override_dir = Path(tmp) / "intake"
            override_dir.mkdir()
            (override_dir / "sytem.md").write_text("typo in filename", encoding="utf-8")

            store = FilePromptStore(_DEFAULTS_DIRS, Path(tmp))
            prompts = await store.get("intake")

        self.assertIn("sytem", prompts.ignored)
        self.assertEqual(prompts.effective, _default_prompts())

    async def test_missing_overrides_dir_is_fine(self):
        store = FilePromptStore(_DEFAULTS_DIRS, Path("/nonexistent"))
        prompts = await store.get("intake")
        self.assertEqual(prompts.effective.keys(), PROMPT_VARIABLES.keys())


class TestRedisPromptStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.store = RedisPromptStore(FilePromptStore(_DEFAULTS_DIRS), self.redis)

    async def test_get_without_overrides_returns_files(self):
        prompts = await self.store.get("intake")
        self.assertEqual(prompts.effective, _default_prompts())
        self.assertEqual(prompts.runtime, {})

    async def test_set_overrides_single_prompt(self):
        await self.store.set("intake", "system", "Runtime override")

        prompts = await self.store.get("intake")
        self.assertEqual(prompts.effective["system"], "Runtime override")
        self.assertEqual(prompts.effective["ticket_human"], _default_prompts()["ticket_human"])
        self.assertEqual(prompts.origins["system"], PromptOrigin.RUNTIME)

    async def test_set_unknown_name_raises(self):
        with self.assertRaises(PromptStoreError):
            await self.store.set("intake", "no_such_prompt", "text")

    async def test_reset_restores_file_value(self):
        await self.store.set("intake", "system", "Runtime override")

        await self.store.reset("intake", "system")

        prompts = await self.store.get("intake")
        self.assertEqual(prompts.effective["system"], _default_prompts()["system"])

    async def test_stale_override_for_removed_prompt_ignored(self):
        await self.redis.hset("prompts:intake", "removed_prompt", "stale")

        prompts = await self.store.get("intake")

        self.assertNotIn("removed_prompt", prompts.effective)
        self.assertIn("removed_prompt", prompts.ignored)


class TestBuildIntakePrompts(unittest.TestCase):
    def test_defaults_are_valid(self):
        prompts = build_intake_prompts(_default_prompts())
        self.assertIn("{service_context}", prompts.ticket_human)

    def test_missing_template_raises(self):
        raw = _default_prompts()
        del raw["ticket_human"]
        with self.assertRaises(ValueError) as ctx:
            build_intake_prompts(raw)
        self.assertIn("ticket_human", str(ctx.exception))

    def test_unknown_placeholder_raises(self):
        raw = _default_prompts()
        raw["ticket_human"] = "Requester: {caler_name}"  # typo
        with self.assertRaises(ValueError) as ctx:
            build_intake_prompts(raw)
        self.assertIn("caler_name", str(ctx.exception))
        self.assertIn("ticket_human", str(ctx.exception))

    def test_unregistered_template_raises(self):
        # A file in prompts/ that nobody added to PROMPT_VARIABLES is offered
        # for editing by the admin API and never reaches the model
        raw = {**_default_prompts(), "future_prompt": "text"}
        with self.assertRaises(ValueError) as ctx:
            build_intake_prompts(raw)
        self.assertIn("future_prompt", str(ctx.exception))

    def test_all_registry_prompts_have_files(self):
        self.assertEqual(_default_prompts().keys(), PROMPT_VARIABLES.keys())

    def test_errors_are_addressed_by_template_name(self):
        # What the admin UI marks and what the startup check routes on
        raw = _default_prompts()
        raw["ticket_human"] = "Requester: {caler_name}"
        with self.assertRaises(PromptValidationError) as ctx:
            build_intake_prompts(raw)
        self.assertEqual(list(ctx.exception.errors), ["ticket_human"])


class TestPackagedDefaults(unittest.IsolatedAsyncioTestCase):
    async def test_every_registered_module_ships_valid_defaults(self):
        """Our own templates, checked for all modules at once.

        The startup check no longer looks at a default a deployment shadowed
        with a working override (REQ-005 R2), so this is where a broken one is
        caught — and where a module nobody wrote a prompt test for is covered.
        """
        registry = build_registry(get_settings())
        modules = [m for m in registry.modules if m.validate_prompts and m.prompts_dir]
        store = FilePromptStore({m.name: m.prompts_dir for m in modules})
        # A module switched off by default registers nothing, and this would
        # quietly become a test of an empty list
        self.assertTrue(modules)

        for module in modules:
            with self.subTest(module=module.name):
                assert module.validate_prompts is not None
                module.validate_prompts((await store.get(module.name)).defaults)


_PROBE_VARIABLES: dict[str, set[str]] = {"greeting": set(), "farewell": set()}


class _ProbePrompts(BaseModel):
    greeting: str
    farewell: str


def _probe_module() -> ModuleInfo:
    return ModuleInfo(
        name="probe",
        description="a module the startup check knows nothing special about",
        prompt_names=tuple(_PROBE_VARIABLES),
        validate_prompts=lambda raw: build_prompts(raw, _PROBE_VARIABLES, _ProbePrompts, module="probe"),
    )


def _write_prompts(directory: Path, **files: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    return directory


class TestStartupPromptCheck(unittest.IsolatedAsyncioTestCase):
    """Where REQ-005 draws its line: by origin of the template, not by the error."""

    async def test_broken_packaged_default_stops_the_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            defaults = _write_prompts(Path(tmp) / "probe", greeting="Hi {nope}", farewell="Bye")

            with self.assertRaises(PromptValidationError) as ctx:
                await check_module_prompts(_probe_module(), FilePromptStore({"probe": defaults}))

        self.assertIn("nope", str(ctx.exception))

    async def test_broken_override_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            defaults = _write_prompts(Path(tmp) / "probe", greeting="Hi", farewell="Bye")
            _write_prompts(Path(tmp) / "overrides" / "probe", greeting="Hi {nope}")
            store = FilePromptStore({"probe": defaults}, Path(tmp) / "overrides")

            with self.assertLogs("itop_ai_assistant.main", level=logging.WARNING) as logs:
                await check_module_prompts(_probe_module(), store)

        self.assertIn("nope", "\n".join(logs.output))

    async def test_a_template_an_override_cannot_have_broken_stops_the_boot(self):
        # A default missing from the artifact, shadowed by an override that now
        # names nothing: the override is dropped, and what is left is ours
        with tempfile.TemporaryDirectory() as tmp:
            defaults = _write_prompts(Path(tmp) / "probe", farewell="Bye")
            _write_prompts(Path(tmp) / "overrides" / "probe", greeting="Hi")
            store = FilePromptStore({"probe": defaults}, Path(tmp) / "overrides")

            with self.assertRaises(PromptValidationError) as ctx:
                await check_module_prompts(_probe_module(), store)

        self.assertEqual(list(ctx.exception.errors), ["greeting"])


if __name__ == "__main__":
    unittest.main()
