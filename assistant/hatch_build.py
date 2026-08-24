"""Bakes the build stamp into the package as `_build_info.py`.

The version itself comes from the git tag via hatch-vcs and ends up in the
wheel metadata, where `importlib.metadata` can read it. The commit and the
build timestamp have no standard metadata field, so they are written into a
generated module instead — the artifact describes itself, and nothing has to
be re-supplied through the environment at run time.

Regenerated on every build, including editable installs; gitignored.
"""

import os
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
channel = {channel!r}
'''

#: What a build calls itself when nobody said otherwise. Only the workflow that
#: publishes an image sets `BUILD_CHANNEL`, so everything else — a checkout, a
#: `uv build`, an image somebody built themselves — is `local` by default
#: rather than by detection.
LOCAL_CHANNEL = "local"


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


def _channel() -> str:
    """Whether this artifact is one we publish, said rather than inferred.

    Telemetry refuses to report from anything that is not `release`
    (`util/build_info.py::is_release_build`), so the answer must not depend on
    the shape of a version string: a checkout sitting exactly on a tag reads a
    clean release number, and `local_scheme` in `pyproject.toml` decides that
    shape for reasons of its own. `.github/workflows/docker-publish.yml` is the
    only place that passes `BUILD_CHANNEL=release`.
    """
    return os.environ.get("BUILD_CHANNEL") or LOCAL_CHANNEL


def _commit(root: Path) -> str | None:
    """Short commit hash: handed over by the image build, or read from git.

    The image build has no repository — CI passes `BUILD_COMMIT` instead.
    Building from a checkout (`uv build`, `uv sync`) asks git directly.
    """
    passed = os.environ.get("BUILD_COMMIT")
    if passed:
        return passed[:7]
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
                channel=_channel(),
            ),
            encoding="utf-8",
        )
