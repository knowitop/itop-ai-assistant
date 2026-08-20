"""Typed prompt set for the intake module.

Default templates live in `agents/intake/prompts/*.md` and can be overridden
per deployment — see `prompt_store.FilePromptStore`.
"""

from pathlib import Path

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel

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
    "catalog_human": {"services"},
    "ticket_human": {"caller_name", "title", "description", "conversation", "service_context", "session_scope"},
}


class IntakePrompts(BaseModel):
    system: str
    system_classify: str
    system_clarify: str
    system_handoff_note: str
    system_similar: str
    catalog_human: str
    ticket_human: str


def build_intake_prompts(raw: dict[str, str]) -> IntakePrompts:
    """Validate raw templates and build the typed prompt set.

    Raises ValueError on missing or unregistered templates, unparseable
    templates or unknown placeholders. Called at startup to fail fast instead
    of crashing on a live ticket; the admin API reuses it to validate edits
    before saving.
    """
    missing = PROMPT_VARIABLES.keys() - raw.keys()
    if missing:
        raise ValueError(f"Missing prompt templates: {sorted(missing)}")
    # A template nobody registered is shown as editable by the admin API and
    # then never reaches the model — silent, and likelier with the system
    # message split across five files.
    unknown = raw.keys() - PROMPT_VARIABLES.keys()
    if unknown:
        raise ValueError(f"Unknown prompt templates: {sorted(unknown)}, known: {sorted(PROMPT_VARIABLES)}")

    errors = []
    for name, allowed in PROMPT_VARIABLES.items():
        try:
            variables = set(PromptTemplate.from_template(raw[name]).input_variables)
        except ValueError as e:
            errors.append(f"{name}: cannot parse template: {e}")
            continue
        unknown = variables - allowed
        if unknown:
            errors.append(f"{name}: unknown placeholders {sorted(unknown)}, allowed: {sorted(allowed)}")
    if errors:
        raise ValueError("Invalid prompt templates:\n" + "\n".join(errors))

    return IntakePrompts(**{name: raw[name] for name in PROMPT_VARIABLES})
