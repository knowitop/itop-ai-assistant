from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from itop_ai_assistant.util.redis_keyspace import PROMPTS_PREFIX

logger = logging.getLogger(__name__)


class PromptStoreError(Exception):
    pass


class PromptOrigin(StrEnum):
    DEFAULT = "default"
    FILE = "file"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class PromptSet:
    """One module's templates, kept in the layers they arrived from.

    The merged text is `effective`; the layers are what tells our own template
    from one the deployment wrote, and that difference is the whole of REQ-005 —
    a broken template of ours refuses the boot, a broken override only warns.

    `ignored` holds overrides naming no packaged prompt. They are dropped rather
    than applied, and reported by name so the admin UI can say which file or
    entry is not being read.
    """

    defaults: dict[str, str]
    files: dict[str, str] = field(default_factory=dict)
    runtime: dict[str, str] = field(default_factory=dict)
    ignored: dict[str, str] = field(default_factory=dict)

    @property
    def effective(self) -> dict[str, str]:
        return {**self.defaults, **self.files, **self.runtime}

    @property
    def origins(self) -> dict[str, PromptOrigin]:
        origins: dict[str, PromptOrigin] = dict.fromkeys(self.defaults, PromptOrigin.DEFAULT)
        origins.update(dict.fromkeys(self.files, PromptOrigin.FILE))
        origins.update(dict.fromkeys(self.runtime, PromptOrigin.RUNTIME))
        return origins


class PromptStore(Protocol):
    """Source of prompt templates for business modules.

    Read once per processing run so a single run always sees a consistent
    set. Priority: runtime overrides (Redis, edited via admin API) >
    per-deployment files (prompts_dir) > packaged defaults.
    """

    async def get(self, module: str) -> PromptSet: ...

    async def set(self, module: str, name: str, text: str) -> None: ...

    async def reset(self, module: str, name: str) -> None: ...


def read_prompt_dir(path: Path) -> dict[str, str]:
    """Read all *.md templates from a directory, keyed by file stem."""
    if not path.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(path.glob("*.md"))}


def _no_such_prompt(module: str, name: str) -> str:
    return f"no packaged prompt named {name!r} in module {module!r}"


class FilePromptStore:
    """Reads prompt templates from disk: customer overrides shadow packaged defaults.

    Defaults come from a `{module: directory}` map — each business module owns
    its own `agents/<module>/prompts/` directory and hands it in via
    `ModuleInfo.prompts_dir`; there is no shared packaged root to derive a
    per-module path from. Overrides stay a single shared deploy directory:
    `overrides_dir/<module>/<prompt_name>.md` shadows a default of the same
    name — the remaining prompts keep their defaults. Files are re-read on
    every call, so prompt edits apply to the next run without a restart.
    """

    def __init__(self, defaults_dirs: Mapping[str, Path], overrides_dir: Path | None = None):
        self._defaults_dirs = defaults_dirs
        self._overrides_dir = overrides_dir

    async def get(self, module: str) -> PromptSet:
        defaults_dir = self._defaults_dirs.get(module)
        if defaults_dir is None:
            raise PromptStoreError(f"No prompt directory registered for module {module!r}")
        defaults = read_prompt_dir(defaults_dir)
        if not defaults:
            raise PromptStoreError(f"No default prompts found in {defaults_dir}")
        if self._overrides_dir is None:
            return PromptSet(defaults=defaults)

        override_dir = self._overrides_dir / module
        overrides = read_prompt_dir(override_dir)
        # An override can only shadow a packaged prompt. A file naming none of
        # them (a typo, a leftover from an older version) is dropped instead of
        # refusing the boot: that would leave the deployment without the admin
        # UI that reports the file (REQ-005).
        ignored = {name: _no_such_prompt(module, name) for name in overrides.keys() - defaults.keys()}
        if ignored:
            logger.warning(
                f"Ignoring prompt override files in {override_dir} naming no packaged prompt: {sorted(ignored)}"
            )
        return PromptSet(
            defaults=defaults,
            files={name: text for name, text in overrides.items() if name in defaults},
            ignored=ignored,
        )

    async def set(self, module: str, name: str, text: str) -> None:
        raise PromptStoreError("FilePromptStore is read-only")

    async def reset(self, module: str, name: str) -> None:
        raise PromptStoreError("FilePromptStore is read-only")


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
        return f"{PROMPTS_PREFIX}{module}"

    async def get(self, module: str) -> PromptSet:
        prompts = await self._files.get(module)
        try:
            stored = await self._redis.hgetall(self._key(module))
        except RedisError as e:
            # Runtime overrides are an enhancement — degrade to file prompts
            logger.warning(f"Redis unavailable, using file prompts for {module!r}: {e}")
            return prompts
        known = prompts.defaults.keys()
        ignored = {name: _no_such_prompt(module, name) for name in stored.keys() - known}
        if ignored:
            logger.warning(f"Ignoring unknown runtime prompt overrides for {module!r}: {sorted(ignored)}")
        return replace(
            prompts,
            runtime={name: text for name, text in stored.items() if name in known},
            ignored={**prompts.ignored, **ignored},
        )

    async def set(self, module: str, name: str, text: str) -> None:
        known = (await self._files.get(module)).defaults
        if name not in known:
            raise PromptStoreError(f"Unknown prompt {name!r} for module {module!r}. Known: {sorted(known)}")
        await self._redis.hset(self._key(module), name, text)

    async def reset(self, module: str, name: str) -> None:
        await self._redis.hdel(self._key(module), name)
