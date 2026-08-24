"""The one rule the whole requirement rests on: what does not check out is `other`.

REQ-009 R4 is a contract about what we never collect — ticket text, names,
URLs, keys, iTop object ids — and a contract like that does not hold by good
intentions at the call sites. It holds because a value that is not recognized
is replaced here, and because the document's other half (`builder.py`) admits
only integers and booleans, which cannot carry prose at all.

Two string values reach the document through this module, guarded differently
on purpose. The LLM provider is a real enumeration — `core/llm_providers.py`
lists every endpoint this build can talk to, so an unknown value is one we did
not ship. The model name is not an enumeration and cannot become one without a
curated list of families that would exist for telemetry alone and need an entry
for every model released anywhere; what guards it is its *shape*. That is the
smaller guarantee of the two, and `model_name` names what it cannot catch.

Nothing else in the document is a string a customer can influence: the build
version and commit come from our own build, and the language is normalized by
`settings/module_locales.py` before it is stored.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from itop_ai_assistant.core.llm_providers import PROVIDERS

#: What replaces anything unrecognized. One constant, so that a reader
#: grepping for it finds every field that can degrade.
OTHER = "other"

#: An owner, a `/` and a name, or just a name: `google/gemma-4-31b-it`,
#: `qwen3-32b`, `llama3.2:3b`. At most one `/` — a location is cut to its last
#: segment first. The length bound is part of the guard: a long value is prose.
_MODEL_NAME = re.compile(r"^(?=.{1,64}$)[a-z0-9._:-]+(?:/[a-z0-9._:-]+)?$")

#: A local path or an object-store URI, told from a registry id by its start or
#: by a second `/`.
_PATH_STARTS = (".", "/")

#: Files a container runtime leaves in the filesystem root. Docker writes the
#: first, Podman the second; neither is present on a bare host.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def llm_provider(value: str | None) -> str | None:
    """The provider id if this build ships one by that name, else `other`.

    `None` for an installation that has not been configured yet — "not set"
    and "set to something we do not know" are different answers, and
    collapsing them would hide a fresh install among the misconfigured ones.
    """
    if not value:
        return None
    return value if value in PROVIDERS else OTHER


def model_name(value: str | None) -> str | None:
    """The model name lowercased if it has the shape of one, else `other`.

    The shape catches the case R4 gives its own example for: a name with a
    comment appended to it, in any alphabet.

    The owner half of a registry id travels with the name. It is the only thing
    separating a model from somebody's rebuild of it — `google/gemma-4-31b-it`
    from `cyankiwi/gemma-4-31b-it-awq-4bit` — and "the original or a community
    quant" is the first question asked of an installation that answers poorly.

    A location keeps only its last segment. `/srv/models/qwen3-32b-awq` and
    `s3://core-llm/llama-3-8b` are what an endpoint serves when nobody named
    the model, so rejecting them would blank the field for exactly the
    installations it exists to describe; the tree or bucket above holds no
    vendor and every chance of naming the company. Whether the administrator
    filled in the right box is not a question asked here — `other` is for a
    value that is no model name in any reading, not for one that belongs in a
    neighbouring field.

    What the shape cannot catch: an opaque token, a bare `host:port` and a
    `host:port/path` are spelled with the characters a model id is, and a name
    can be the customer's own — a fine-tune in a private repository under the
    company's name, a directory called after what it serves. R4 holds here by
    form, and a name somebody chose can always carry what the form permits.
    """
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered.startswith(_PATH_STARTS) or lowered.count("/") > 1:
        lowered = lowered.rpartition("/")[2]
    return lowered if _MODEL_NAME.match(lowered) else OTHER


def python_version() -> str:
    """`"3.13"` — the interpreter's major and minor, and no patch level.

    The patch level answers no question the requirement asks and would split
    every installation into a series of near-identical ones.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def utc_offset_minutes() -> int:
    """This host's offset from UTC, in minutes, right now.

    Minutes rather than hours: not every offset is a whole hour. Read at build
    time rather than cached — a document built after a DST change should say
    what is true then.
    """
    offset = datetime.now().astimezone().utcoffset()
    return 0 if offset is None else int(offset.total_seconds() // 60)


def in_container() -> bool:
    """Whether this process runs in a container, as far as the filesystem says."""
    return any(marker.exists() for marker in _CONTAINER_MARKERS)
