from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class PromptStoreError(Exception):
    pass


class PromptStore(Protocol):
    """Source of prompt templates for business modules.

    Read once per processing run so a single run always sees a consistent
    set. Priority: runtime overrides (Redis, edited via admin API) >
    per-deployment files (prompts_dir) > packaged defaults.
    """

    async def get(self, module: str) -> dict[str, str]: ...

    async def set(self, module: str, name: str, text: str) -> None: ...

    async def reset(self, module: str, name: str) -> None: ...

    async def overrides(self, module: str) -> frozenset[str]:
        """Names of prompts currently overridden at runtime."""
        ...


def read_prompt_dir(path: Path) -> dict[str, str]:
    """Read all *.md templates from a directory, keyed by file stem."""
    if not path.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(path.glob("*.md"))}


class FilePromptStore:
    """Reads prompt templates from disk: customer overrides shadow packaged defaults.

    Layout: `<dir>/<module>/<prompt_name>.md`. A customer overrides a single
    prompt by placing a file with the same name under `overrides_dir` — the
    remaining prompts keep their defaults. Files are re-read on every call,
    so prompt edits apply to the next run without a restart.
    """

    def __init__(self, defaults_dir: Path, overrides_dir: Path | None = None):
        self._defaults_dir = defaults_dir
        self._overrides_dir = overrides_dir

    async def get(self, module: str) -> dict[str, str]:
        prompts = read_prompt_dir(self._defaults_dir / module)
        if not prompts:
            raise PromptStoreError(f"No default prompts found in {self._defaults_dir / module}")

        if self._overrides_dir:
            overrides = read_prompt_dir(self._overrides_dir / module)
            unknown = overrides.keys() - prompts.keys()
            if unknown:
                raise PromptStoreError(
                    f"Unknown prompt overrides in {self._overrides_dir / module}: {sorted(unknown)}. "
                    f"Known prompts: {sorted(prompts)}"
                )
            prompts.update(overrides)

        return prompts

    async def set(self, module: str, name: str, text: str) -> None:
        raise PromptStoreError("FilePromptStore is read-only")

    async def reset(self, module: str, name: str) -> None:
        raise PromptStoreError("FilePromptStore is read-only")

    async def overrides(self, module: str) -> frozenset[str]:
        return frozenset()


class RedisPromptStore:
    """Runtime prompt overrides in Redis on top of a file-based store.

    Edits made through the admin API land here and apply from the next run.
    Placeholder validation is the caller's job (see the module's
    `validate_prompts` in its ModuleInfo) — the store only guards names.
    """

    def __init__(self, files: FilePromptStore, redis: Redis):
        self._files = files
        self._redis = redis

    def _key(self, module: str) -> str:
        return f"prompts:{module}"

    async def get(self, module: str) -> dict[str, str]:
        prompts = await self._files.get(module)
        try:
            stored = await self._redis.hgetall(self._key(module))
        except RedisError as e:
            # Runtime overrides are an enhancement — degrade to file prompts
            logger.warning(f"Redis unavailable, using file prompts for {module!r}: {e}")
            return prompts
        unknown = stored.keys() - prompts.keys()
        if unknown:
            logger.warning(f"Ignoring unknown runtime prompt overrides for {module!r}: {sorted(unknown)}")
        prompts.update({name: text for name, text in stored.items() if name in prompts})
        return prompts

    async def set(self, module: str, name: str, text: str) -> None:
        known = await self._files.get(module)
        if name not in known:
            raise PromptStoreError(f"Unknown prompt {name!r} for module {module!r}. Known: {sorted(known)}")
        await self._redis.hset(self._key(module), name, text)

    async def reset(self, module: str, name: str) -> None:
        await self._redis.hdel(self._key(module), name)

    async def overrides(self, module: str) -> frozenset[str]:
        stored = await self._redis.hgetall(self._key(module))
        return frozenset(stored.keys())
