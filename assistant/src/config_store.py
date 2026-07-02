"""Runtime-editable business configuration.

Module config = settings defaults (env/yaml) + JSON overrides stored in
Redis by the admin API. Read once per processing run so a single run always
sees a consistent snapshot; edits apply from the next run without restart.
"""

import json
import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from config import Settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "config:"

TConfig = TypeVar("TConfig", bound=BaseModel)


class ConfigStore(Protocol):
    async def get(self, module: str, model: type[TConfig]) -> TConfig: ...

    async def set(self, module: str, values: dict, model: type[TConfig]) -> TConfig: ...

    async def reset(self, module: str) -> None: ...


class RedisConfigStore:
    """Serves module config with Redis overrides on top of settings defaults.

    The module name must match the settings attribute holding its defaults
    (e.g. module "enrichment" → `settings.enrichment`).
    """

    def __init__(self, redis: Redis, settings: Settings):
        self._redis = redis
        self._settings = settings

    def _defaults(self, module: str) -> BaseModel:
        return getattr(self._settings, module)

    async def get(self, module: str, model: type[TConfig]) -> TConfig:
        defaults = self._defaults(module)
        try:
            raw = await self._redis.get(_KEY_PREFIX + module)
        except RedisError as e:
            # Runtime overrides are an enhancement — degrade to defaults
            logger.warning(f"Redis unavailable, using default config for {module!r}: {e}")
            return defaults  # type: ignore[return-value]
        if not raw:
            return defaults  # type: ignore[return-value]
        try:
            overrides = json.loads(raw)
            return model(**{**defaults.model_dump(), **overrides})
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(f"Stored config for {module!r} is invalid, falling back to defaults: {e}")
            return defaults  # type: ignore[return-value]

    async def set(self, module: str, values: dict, model: type[TConfig]) -> TConfig:
        """Validate values merged over defaults and store the full result.

        Raises pydantic.ValidationError on invalid values — the admin API
        surfaces it to the client before anything is written.
        """
        defaults = self._defaults(module)
        validated = model(**{**defaults.model_dump(), **values})
        await self._redis.set(_KEY_PREFIX + module, validated.model_dump_json())
        return validated

    async def reset(self, module: str) -> None:
        await self._redis.delete(_KEY_PREFIX + module)
