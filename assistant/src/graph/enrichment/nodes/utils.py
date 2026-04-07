import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove <think>…</think> reasoning blocks emitted by reasoning models."""
    return _THINK_RE.sub("", text).strip()
