import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
from pydantic import ValidationError
from redis.exceptions import RedisError

from config import EnrichmentConfig
from config_store import RedisConfigStore


def _make_store() -> tuple[RedisConfigStore, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = SimpleNamespace(enrichment=EnrichmentConfig())
    return RedisConfigStore(redis, settings), redis


class TestRedisConfigStore(unittest.IsolatedAsyncioTestCase):
    async def test_get_without_overrides_returns_defaults(self):
        store, _ = _make_store()

        cfg = await store.get("enrichment", EnrichmentConfig)

        self.assertEqual(cfg, EnrichmentConfig())

    async def test_set_then_get_applies_overrides(self):
        store, _ = _make_store()

        await store.set("enrichment", {"max_rounds": 5}, EnrichmentConfig)
        cfg = await store.get("enrichment", EnrichmentConfig)

        self.assertEqual(cfg.max_rounds, 5)
        # Untouched fields keep their defaults
        self.assertEqual(cfg.max_classify_rounds, 2)

    async def test_set_invalid_values_raises_and_stores_nothing(self):
        store, redis = _make_store()

        with self.assertRaises(ValidationError):
            await store.set("enrichment", {"max_rounds": "not-a-number"}, EnrichmentConfig)

        self.assertIsNone(await redis.get("config:enrichment"))

    async def test_reset_restores_defaults(self):
        store, _ = _make_store()
        await store.set("enrichment", {"max_rounds": 5}, EnrichmentConfig)

        await store.reset("enrichment")
        cfg = await store.get("enrichment", EnrichmentConfig)

        self.assertEqual(cfg.max_rounds, 2)

    async def test_corrupt_stored_value_falls_back_to_defaults(self):
        store, redis = _make_store()
        await redis.set("config:enrichment", "{not json")

        cfg = await store.get("enrichment", EnrichmentConfig)

        self.assertEqual(cfg, EnrichmentConfig())

    async def test_redis_error_on_get_falls_back_to_defaults(self):
        store, redis = _make_store()
        with patch.object(redis, "get", AsyncMock(side_effect=RedisError("down"))):
            cfg = await store.get("enrichment", EnrichmentConfig)

        self.assertEqual(cfg, EnrichmentConfig())


if __name__ == "__main__":
    unittest.main()
