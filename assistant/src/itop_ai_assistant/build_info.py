"""Which build is this — read from the artifact, never from the environment.

`_build_info.py` is generated at build time (see `hatch_build.py`); the wheel
metadata carries the same version. Both are absent only when the package was
never built — an editable install straight from a checkout — and then the
answer is an honest "dev".
"""

from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version

DISTRIBUTION = "itop-ai-assistant"
UNKNOWN_VERSION = "dev"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str | None
    built_at: str | None


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    try:
        from itop_ai_assistant import _build_info
    except ImportError:
        return BuildInfo(version=_metadata_version(), commit=None, built_at=None)
    return BuildInfo(
        version=_build_info.version or _metadata_version(),
        commit=_build_info.commit,
        built_at=_build_info.built_at,
    )


def _metadata_version() -> str:
    try:
        return metadata_version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
