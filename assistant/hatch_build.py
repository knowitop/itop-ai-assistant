"""Bakes the build stamp into the package as `_build_info.py`.

The version itself comes from the git tag via hatch-vcs and ends up in the
wheel metadata, where `importlib.metadata` can read it. The commit and the
build timestamp have no standard metadata field, so they are written into a
generated module instead — the artifact describes itself, and nothing has to
be re-supplied through the environment at run time.

Regenerated on every build, including editable installs; gitignored.
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

TARGET = Path("src") / "itop_ai_assistant" / "_build_info.py"

TEMPLATE = '''"""Generated at build time by hatch_build.py — do not edit."""

version = {version!r}
commit = {commit!r}
built_at = {built_at!r}
'''


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _commit(root: Path) -> str | None:
    """Short commit hash.

    Deliberately no dirty marker: the image build copies `.git` next to
    `assistant/` alone, so git sees the rest of the repository as deleted and
    every build would claim to be dirty.
    """
    return _git(root, "rev-parse", "--short=7", "HEAD") or None


class BuildInfoHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        (root / TARGET).write_text(
            TEMPLATE.format(
                version=self.metadata.version,
                commit=_commit(root),
                built_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            ),
            encoding="utf-8",
        )
