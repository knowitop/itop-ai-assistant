import re

from bs4 import BeautifulSoup
from langchain_core.messages import AIMessage, HumanMessage
from markdownify import markdownify

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def bind_oql(oql: str, this: dict[str, str | None]) -> str:
    """Substitute :this->field placeholders in an OQL template string."""
    for key, value in this.items():
        oql = oql.replace(f":this->{key}", "NULL" if value is None else value)
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
