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

#: The channel a build claims when it was not stamped as anything: an editable
#: install, a `uv build`, an image somebody built themselves.
LOCAL_CHANNEL = "local"

#: The one channel telemetry reports from — stamped only by the workflow that
#: publishes an image (`hatch_build.py::_channel`).
RELEASE_CHANNEL = "release"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str | None
    built_at: str | None
    #: `release` for an artifact we published, `local` for everything else.
    channel: str = LOCAL_CHANNEL


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
        # `getattr`, because a stamp written by an older `hatch_build.py` has
        # no such field and the honest reading of its silence is `local`.
        channel=getattr(_build_info, "channel", LOCAL_CHANNEL),
    )


def _metadata_version() -> str:
    try:
        return metadata_version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


def is_release_build() -> bool:
    """Is this a build we published, rather than a checkout or a local image?

    Asked by telemetry, which must not report from a developer's machine
    (REQ-009 R5): with the switch on by default, every clone that finished the
    setup wizard would otherwise inflate the count of installations — the one
    number the requirement exists to produce.

    The build says so itself. It is not read off the version, which only ever
    approximated the question: `local_scheme` in `pyproject.toml` decides
    whether a checkout's version carries a local segment, so a clone sitting
    exactly on a tag reads a clean `0.5.0` — dirty working tree included — and
    would pass a test on shape. `BUILD_CHANNEL=release` is passed by one line
    of `.github/workflows/docker-publish.yml` and by nothing else.

    A build that is not `release` can still send when somebody means it:
    `TELEMETRY_ALLOW_UNPUBLISHED_BUILD` is that switch, and it is separate from
    `TELEMETRY_TEST_MODE`, which only marks what is sent (`telemetry/sender.py`).
    Without it, an installation deployed from source never reports and is never
    counted — a cost named rather than guessed around, since nothing here can
    tell such a server from a laptop.
    """
    return get_build_info().channel == RELEASE_CHANNEL
