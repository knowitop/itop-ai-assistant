"""Typed prompt set for the intake module.

Default templates live in `agents/intake/prompts/*.md` and can be overridden
per deployment — see `prompt_store.FilePromptStore`.
"""

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from itop_ai_assistant.settings.prompt_validation import build_prompts

#: This module's own identifier — the config section it owns and the
#: prompt-store lookup key. Declared here, not in `pipeline.py`, so that
#: `compose.py`/`run.py` can import it without a cycle through `run.py`'s own
#: import of `compose.py`.
MODULE = "intake"

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Allowed placeholders per template — everything `prompt.py` passes when it
# builds the agent's initial messages. `conversation` is a rendered XML block,
# not a MessagesPlaceholder.
#
# The system message comes from the first five: a base plus one fragment per
# switchable action, joined by `prompt.build_system_prompt`. `system` names the
# base, so a deployment that overrode the system prompt keeps overriding it.
PROMPT_VARIABLES: dict[str, set[str]] = {
    "system": set(),
    "system_classify": set(),
    "system_clarify": set(),
    "system_handoff_note": set(),
    "system_similar": set(),
    "system_faq": set(),
    "catalog_human": {"services"},
    "ticket_human": {"caller_name", "title", "description", "conversation", "service_context", "session_scope"},
}


class IntakePrompts(BaseModel):
    system: str
    system_classify: str
    system_clarify: str
    system_handoff_note: str
    system_similar: str
    system_faq: str
    catalog_human: str
    ticket_human: str


def build_intake_prompts(raw: Mapping[str, str]) -> IntakePrompts:
    """Validate raw templates and build the typed prompt set.

    Raises `PromptValidationError` on missing or unregistered templates,
    unparseable templates or unknown placeholders. Called at startup to fail
    fast instead of crashing on a live ticket; the admin API reuses it to
    validate an edit before saving.
    """
    return build_prompts(raw, PROMPT_VARIABLES, IntakePrompts, module=MODULE)
