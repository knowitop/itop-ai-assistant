import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str | None) -> str:
    """Remove <think>…</think> reasoning blocks emitted by reasoning models."""
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()
