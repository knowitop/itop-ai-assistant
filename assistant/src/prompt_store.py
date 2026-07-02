from pathlib import Path
from typing import Protocol


class PromptStoreError(Exception):
    pass


class PromptStore(Protocol):
    """Source of prompt templates for business modules.

    Read once per processing run so a single run always sees a consistent
    set. File-based for now; a UI-editable store (e.g. Redis-backed) can
    replace it without touching the processing code.
    """

    async def get(self, module: str) -> dict[str, str]: ...


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
