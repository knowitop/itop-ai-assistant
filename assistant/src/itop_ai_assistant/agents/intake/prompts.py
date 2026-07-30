"""Typed prompt set for the intake module.

Default templates live in `prompts/intake/*.md` (app root) and can be
overridden per deployment — see `prompt_store.FilePromptStore`.
"""

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel

# Allowed placeholders per template — everything `prompt.py` passes when it
# builds the agent's initial messages. `conversation` is a rendered XML block,
# not a MessagesPlaceholder.
PROMPT_VARIABLES: dict[str, set[str]] = {
    "system": set(),
    "catalog_human": {"services"},
    "ticket_human": {"caller_name", "title", "description", "conversation", "service_context"},
}


class IntakePrompts(BaseModel):
    system: str
    catalog_human: str
    ticket_human: str


def build_intake_prompts(raw: dict[str, str]) -> IntakePrompts:
    """Validate raw templates and build the typed prompt set.

    Raises ValueError on missing templates, unparseable templates or unknown
    placeholders. Called at startup to fail fast instead of crashing on a
    live ticket; the admin API reuses it to validate edits before saving.
    """
    missing = PROMPT_VARIABLES.keys() - raw.keys()
    if missing:
        raise ValueError(f"Missing prompt templates: {sorted(missing)}")

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
