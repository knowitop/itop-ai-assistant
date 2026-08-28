import unittest
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
from pydantic import ValidationError
from redis.exceptions import RedisError

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.config import Settings
from itop_ai_assistant.settings.config_store import RedisConfigStore
from itop_ai_assistant.vector import VectorConfig


def _make_store() -> tuple[RedisConfigStore, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings()
    return RedisConfigStore(redis, settings), redis


class TestRedisConfigStore(unittest.IsolatedAsyncioTestCase):
    async def test_get_without_overrides_returns_defaults(self):
        store, _ = _make_store()

        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg, IntakeConfig())

    async def test_set_then_get_applies_overrides(self):
        store, _ = _make_store()

        await store.set("intake", {"max_questions": 5}, IntakeConfig)
        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg.max_questions, 5)
        # Untouched fields keep their defaults
        self.assertEqual(cfg.max_classify_questions, 2)

    async def test_set_invalid_values_raises_and_stores_nothing(self):
        store, redis = _make_store()

        with self.assertRaises(ValidationError):
            await store.set("intake", {"max_questions": "not-a-number"}, IntakeConfig)

        self.assertIsNone(await redis.get("config:intake"))

    async def test_reset_restores_defaults(self):
        store, _ = _make_store()
        await store.set("intake", {"max_questions": 5}, IntakeConfig)

        await store.reset("intake")
        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg.max_questions, 3)

    async def test_reset_of_named_fields_leaves_the_rest_overridden(self):
        store, _ = _make_store()
        await store.set("intake", {"max_questions": 5, "max_classify_questions": 1}, IntakeConfig)

        await store.reset("intake", ["max_questions"])
        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg.max_questions, 3)
        self.assertEqual(cfg.max_classify_questions, 1)

    async def test_corrupt_stored_value_falls_back_to_defaults(self):
        store, redis = _make_store()
        await redis.set("config:intake", "{not json")

        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg, IntakeConfig())

    async def test_redis_error_on_get_falls_back_to_defaults(self):
        store, redis = _make_store()
        with patch.object(redis, "get", AsyncMock(side_effect=RedisError("down"))):
            cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg, IntakeConfig())

    async def test_resolves_a_section_settings_has_no_attribute_for(self):
        # `Settings` carries no `intake` field at all (TASK-024) — this is the
        # only path business-module defaults can come through, not a fallback
        # that happens to agree with an unused attribute.
        settings = Settings()
        self.assertFalse(hasattr(settings, "intake"))
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = RedisConfigStore(redis, settings)

        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg, IntakeConfig())

    async def test_env_yaml_module_config_flows_through_as_defaults(self):
        # Constructor kwargs are not a settings source here
        # (settings_customise_sources excludes init_settings) — env is the
        # real path, same as every other complex field (e.g. LLM_PARAMS).
        with patch.dict("os.environ", {"MODULE_CONFIG": '{"intake": {"max_questions": 7}}'}, clear=True):
            settings = Settings(_env_file=None)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = RedisConfigStore(redis, settings)

        cfg = await store.get("intake", IntakeConfig)

        self.assertEqual(cfg.max_questions, 7)

    async def test_vector_resolves_like_a_business_module_section(self):
        # TASK-036: `vector` is infrastructure, not a module, but `Settings`
        # carries no attribute for it either — same fallback, same test as
        # `test_resolves_a_section_settings_has_no_attribute_for` above.
        settings = Settings()
        self.assertFalse(hasattr(settings, "vector"))
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = RedisConfigStore(redis, settings)

        cfg = await store.get("vector", VectorConfig)

        self.assertEqual(cfg, VectorConfig())


if __name__ == "__main__":
    unittest.main()
