import re

from bs4 import BeautifulSoup
from markdownify import markdownify

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str | None) -> str:
    """Remove <think>…</think> reasoning blocks emitted by reasoning models."""
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


def html_to_markdown(text: str | None) -> str:
    """Convert HTML to Markdown, preserving structure for LLM consumption."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return markdownify(str(soup)).strip()
