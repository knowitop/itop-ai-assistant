import re

from bs4 import BeautifulSoup
from langchain_core.messages import AIMessage, HumanMessage
from markdownify import markdownify

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


_NUMERIC_RE = re.compile(r"-?\d+(\.\d+)?")


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


def build_conversation(entries: list, ai_name: str, caller_name: str) -> list:
    """Convert case log entries into a list of LangChain messages."""
    messages = []
    for e in entries:
        if e["user_login"] == ai_name:
            messages.append(AIMessage(content=e["message"]))
        else:
            user_prefix = e["user_login"]
            if e["user_login"] == caller_name:
                user_prefix += " [Requester]"
            messages.append(HumanMessage(content=f"{user_prefix}: {e['message']}", name=e["user_login"]))
    return messages
