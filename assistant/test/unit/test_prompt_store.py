import tempfile
import unittest
from pathlib import Path

import fakeredis.aioredis

from graph.enrichment.prompts import PROMPT_VARIABLES, build_enrichment_prompts
from prompt_store import FilePromptStore, PromptStoreError, RedisPromptStore, read_prompt_dir

_DEFAULTS_DIR = Path(__file__).parents[2] / "prompts"


def _default_prompts() -> dict[str, str]:
    return read_prompt_dir(_DEFAULTS_DIR / "enrichment")


class TestFilePromptStore(unittest.IsolatedAsyncioTestCase):
    async def test_loads_packaged_defaults(self):
        store = FilePromptStore(_DEFAULTS_DIR)
        prompts = await store.get("enrichment")
        self.assertEqual(prompts.keys(), PROMPT_VARIABLES.keys())

    async def test_missing_module_raises(self):
        store = FilePromptStore(_DEFAULTS_DIR)
        with self.assertRaises(PromptStoreError):
            await store.get("no_such_module")

    async def test_override_shadows_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_dir = Path(tmp) / "enrichment"
            override_dir.mkdir()
            (override_dir / "enrich_system.md").write_text("Custom system prompt", encoding="utf-8")

            store = FilePromptStore(_DEFAULTS_DIR, Path(tmp))
            prompts = await store.get("enrichment")

        self.assertEqual(prompts["enrich_system"], "Custom system prompt")
        # Non-overridden prompts keep their defaults
        self.assertEqual(prompts["evaluate_system"], _default_prompts()["evaluate_system"])

    async def test_unknown_override_name_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            override_dir = Path(tmp) / "enrichment"
            override_dir.mkdir()
            (override_dir / "enirch_system.md").write_text("typo in filename", encoding="utf-8")

            store = FilePromptStore(_DEFAULTS_DIR, Path(tmp))
            with self.assertRaises(PromptStoreError) as ctx:
                await store.get("enrichment")

        self.assertIn("enirch_system", str(ctx.exception))

    async def test_missing_overrides_dir_is_fine(self):
        store = FilePromptStore(_DEFAULTS_DIR, Path("/nonexistent"))
        prompts = await store.get("enrichment")
        self.assertEqual(prompts.keys(), PROMPT_VARIABLES.keys())


class TestRedisPromptStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.store = RedisPromptStore(FilePromptStore(_DEFAULTS_DIR), self.redis)

    async def test_get_without_overrides_returns_files(self):
        prompts = await self.store.get("enrichment")
        self.assertEqual(prompts, _default_prompts())
        self.assertEqual(await self.store.overrides("enrichment"), frozenset())

    async def test_set_overrides_single_prompt(self):
        await self.store.set("enrichment", "enrich_system", "Runtime override")

        prompts = await self.store.get("enrichment")
        self.assertEqual(prompts["enrich_system"], "Runtime override")
        self.assertEqual(prompts["evaluate_system"], _default_prompts()["evaluate_system"])
        self.assertEqual(await self.store.overrides("enrichment"), frozenset({"enrich_system"}))

    async def test_set_unknown_name_raises(self):
        with self.assertRaises(PromptStoreError):
            await self.store.set("enrichment", "no_such_prompt", "text")

    async def test_reset_restores_file_value(self):
        await self.store.set("enrichment", "enrich_system", "Runtime override")

        await self.store.reset("enrichment", "enrich_system")

        prompts = await self.store.get("enrichment")
        self.assertEqual(prompts["enrich_system"], _default_prompts()["enrich_system"])

    async def test_stale_override_for_removed_prompt_ignored(self):
        await self.redis.hset("prompts:enrichment", "removed_prompt", "stale")

        prompts = await self.store.get("enrichment")

        self.assertNotIn("removed_prompt", prompts)


class TestBuildEnrichmentPrompts(unittest.TestCase):
    def test_defaults_are_valid(self):
        prompts = build_enrichment_prompts(_default_prompts())
        self.assertIn("{service_context}", prompts.evaluate_system)

    def test_missing_template_raises(self):
        raw = _default_prompts()
        del raw["evaluate_system"]
        with self.assertRaises(ValueError) as ctx:
            build_enrichment_prompts(raw)
        self.assertIn("evaluate_system", str(ctx.exception))

    def test_unknown_placeholder_raises(self):
        raw = _default_prompts()
        raw["evaluate_human"] = "Requester: {caler_name}"  # typo
        with self.assertRaises(ValueError) as ctx:
            build_enrichment_prompts(raw)
        self.assertIn("caler_name", str(ctx.exception))
        self.assertIn("evaluate_human", str(ctx.exception))

    def test_extra_key_in_raw_is_ignored(self):
        raw = {**_default_prompts(), "future_prompt": "text"}
        prompts = build_enrichment_prompts(raw)
        self.assertFalse(hasattr(prompts, "future_prompt"))

    def test_all_registry_prompts_have_files(self):
        self.assertEqual(_default_prompts().keys(), PROMPT_VARIABLES.keys())


if __name__ == "__main__":
    unittest.main()
