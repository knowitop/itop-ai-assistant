"""Generic text and OQL helpers shared across the application.

Deliberately dependency-light and business-agnostic: infrastructure code
(vector indexer, repositories) and business modules alike use these without
importing each other.
"""

import re
from datetime import UTC, datetime
from functools import lru_cache

from bs4 import BeautifulSoup
from markdownify import markdownify

_NUMERIC_RE = re.compile(r"-?\d+(\.\d+)?")

ITOP_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# <think> is the de-facto standard for open-weight reasoning models
# (DeepSeek-R1, Qwen3, QwQ); <thinking> and <reasoning> appear in fine-tunes.
# Overridable via the llm_think_tags setting.
DEFAULT_THINK_TAGS: tuple[str, ...] = ("think", "thinking", "reasoning")


def bind_oql(oql: str, this: dict) -> str:
    """Substitute :this->field placeholders in an OQL template string.

    Non-numeric values are quoted and escaped to prevent OQL injection.
    """
    # Longest keys first so :this->org never matches inside :this->org_id.
    for key in sorted(this, key=len, reverse=True):
        placeholder = f":this->{key}"
        if placeholder not in oql:
            continue
        value = this[key]
        if value is None:
            replacement = "NULL"
        else:
            text = str(value)
            if _NUMERIC_RE.fullmatch(text):
                replacement = text
            else:
                escaped = text.replace("\\", "\\\\").replace('"', '\\"')
                replacement = f'"{escaped}"'
        oql = oql.replace(placeholder, replacement)
    return oql


# TODO: set itop timezone in config
def parse_itop_dt(value) -> datetime | None:
    """Parse an iTop timestamp, tolerating garbage (None on failure).

    iTop returns naive strings in the *server's local time*. We tag them UTC
    purely as a label — timestamptz columns require aware datetimes — and only
    ever compare them with other iTop timestamps, so the offset is irrelevant.
    Do not "fix" this by converting to real UTC: there is no reliable way to
    know the iTop server's zone from here, and consistency is all we need.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, ITOP_DATETIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def html_to_markdown(text: str | None) -> str:
    """Convert HTML to Markdown, preserving structure for LLM consumption."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # An <img> leaves the document but its alt text stays: the picture itself
    # is noise for an embedding and for a prompt, and a screenshot pasted into
    # the body as data:image/...;base64 is unrolled by markdownify into
    # megabytes of "text" — while the alt is often the only description of a
    # diagram a KB article has.
    for image in soup("img"):
        alt = image.get("alt")
        image.replace_with(alt if isinstance(alt, str) else "")
    # The same URI also arrives as an attribute — an <a href>, a <video src>,
    # a <source src>. The address is dropped, not the node: the link's own
    # text ("see attachment") is worth keeping. Any attribute of any tag
    # rather than a list of known ones: which tags markdownify carries into
    # the output changes between its versions.
    for tag in soup.find_all(True):
        for name, value in list(tag.attrs.items()):
            # Multi-valued attributes (class, rel) come back as a list.
            if isinstance(value, str) and value.strip().lower().startswith("data:"):
                del tag[name]
    return markdownify(str(soup)).strip()


@lru_cache
def _think_patterns(tags: tuple[str, ...]) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    alt = "|".join(re.escape(tag) for tag in tags)
    return (
        # Balanced <tag>…</tag> blocks
        re.compile(rf"<({alt})>.*?</\1>", re.DOTALL | re.IGNORECASE),
        # Orphan closing tag: some chat templates emit the opening <think> as
        # part of the prompt, so the completion starts mid-reasoning and ends
        # with </think>.
        re.compile(rf"^.*?</({alt})>", re.DOTALL | re.IGNORECASE),
        # Unclosed opening tag (truncated output): reasoning must not leak.
        re.compile(rf"<({alt})>.*$", re.DOTALL | re.IGNORECASE),
    )


def strip_thinking(content: str | list | None, tags: tuple[str, ...] = DEFAULT_THINK_TAGS) -> str:
    """Remove <think>…</think> reasoning blocks emitted by reasoning models.

    Accepts message content as returned by LangChain: a plain string or a
    list of content blocks (strings or {"type": "text", "text": ...} dicts).
    `tags` lists the tag names to strip (incl. orphan halves); an empty tuple
    disables stripping.
    """
    if not content:
        return ""
    if isinstance(content, list):
        content = "".join(
            block if isinstance(block, str) else str(block.get("text", "")) if isinstance(block, dict) else ""
            for block in content
        )
    if not tags:
        return content.strip()
    pair_re, orphan_close_re, orphan_open_re = _think_patterns(tags)
    text = pair_re.sub("", content)
    text = orphan_close_re.sub("", text)
    text = orphan_open_re.sub("", text)
    return text.strip()
