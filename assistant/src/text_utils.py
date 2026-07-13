"""Generic text and OQL helpers shared across the application.

Deliberately dependency-light and business-agnostic: infrastructure code
(vector indexer, repositories) uses these without importing business modules.
`graph/enrichment/nodes/utils.py` re-exports them for its callers.
"""

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

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


def html_to_markdown(text: str | None) -> str:
    """Convert HTML to Markdown, preserving structure for LLM consumption."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return markdownify(str(soup)).strip()
