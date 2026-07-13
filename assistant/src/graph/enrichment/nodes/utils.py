import re
from functools import lru_cache

from langchain_core.messages import AIMessage, HumanMessage

from domain.ticket import LogEntry

# Re-exported from text_utils (their new home) — existing imports keep working.
from text_utils import bind_oql, html_to_markdown  # noqa: F401

# <think> is the de-facto standard for open-weight reasoning models
# (DeepSeek-R1, Qwen3, QwQ); <thinking> and <reasoning> appear in fine-tunes.
# Overridable via the llm_think_tags setting.
DEFAULT_THINK_TAGS: tuple[str, ...] = ("think", "thinking", "reasoning")


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


def extract_xml_field(text: str, tag: str) -> str | None:
    """Return the trimmed content of the first <tag>…</tag> block, or None."""
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip()
    return value if value else None


def drop_xml_field(text: str, tag: str) -> str:
    """Remove all <tag>…</tag> blocks from the text."""
    return re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def build_conversation(entries: list[LogEntry], ai_name: str, caller_name: str) -> list:
    """Convert case log entries into a list of LangChain messages."""
    messages = []
    for e in entries:
        if e.user_login == ai_name:
            messages.append(AIMessage(content=e.message))
        else:
            user_prefix = e.user_login
            if e.user_login == caller_name:
                user_prefix += " [Requester]"
            messages.append(HumanMessage(content=f"{user_prefix}: {e.message}", name=e.user_login))
    return messages
