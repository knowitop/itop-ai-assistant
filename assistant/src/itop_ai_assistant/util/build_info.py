"""Which build is this — read from the artifact, never from the environment.

`_build_info.py` is generated at build time (see `hatch_build.py`); the wheel
metadata carries the same version. Both are absent only when the package was
never built — an editable install straight from a checkout — and then the
answer is an honest "dev".
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version

DISTRIBUTION = "itop-ai-assistant"
UNKNOWN_VERSION = "dev"

#: A release number and nothing else — no `.dev`, no `+local`, no `rc`.
_RELEASE_VERSION = re.compile(r"^\d+(\.\d+)*$")

#: `fallback-version` from `pyproject.toml`: what hatch-vcs answers when there
#: is no repository to read a tag from. It has the shape of a release and is
#: the opposite of one.
_NO_REPOSITORY = "0.0.0"


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


def is_release_build() -> bool:
    """Is this a build we published, rather than a checkout or a local image?

    Asked by telemetry, which must not report from a developer's machine
    (REQ-009 R5): with the switch on by default, every clone that finished the
    setup wizard would otherwise inflate the count of installations — the one
    number the requirement exists to produce.

    The version answers this and the commit does not. `hatch_build.py` stamps a
    commit for an editable install too, so a checkout has one; but the version
    comes from the git tag through hatch-vcs, and images are published from
    tags alone (`.github/workflows/docker-publish.yml` runs on `push: tags` and
    passes the semver through `SETUPTOOLS_SCM_PRETEND_VERSION`). A checkout
    between tags therefore reads `0.4.1.dev5`, a locally built image reads the
    no-repository fallback, and only a published artifact reads `0.4.1`.

    The cost is named rather than worked around: an installation deployed from
    source onto a real server never reports and is never counted. Nothing here
    can tell it from a laptop — both say `0.4.1.dev5` — and guessing would be
    worse than the silence. `TELEMETRY_TEST_MODE` is the way to send from such
    a build deliberately, marked as test.
    """
    version = get_build_info().version
    return version != _NO_REPOSITORY and _RELEASE_VERSION.match(version) is not None
