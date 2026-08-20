"""Typed prompt set for the selfcheck module.

Default template lives in `agents/selfcheck/prompts/greeting.md` and can be
overridden per deployment — see `prompt_store.FilePromptStore`.
"""

from pathlib import Path

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel

#: This module's own identifier — the config section it owns and the
#: prompt-store lookup key.
MODULE = "selfcheck"

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Allowed placeholders per template — everything `pipeline.py` passes when it
# builds the message.
PROMPT_VARIABLES: dict[str, set[str]] = {
    "greeting": {"services"},
}


class SelfCheckPrompts(BaseModel):
    greeting: str


def build_selfcheck_prompts(raw: dict[str, str]) -> SelfCheckPrompts:
    """Validate raw templates and build the typed prompt set.

    Same contract as every module's: raises ValueError on missing templates,
    unparseable templates or unknown placeholders. Called at startup and by
    the admin API before saving an edit.
    """
    missing = PROMPT_VARIABLES.keys() - raw.keys()
    if missing:
        raise ValueError(f"Missing prompt templates: {sorted(missing)}")
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

    return SelfCheckPrompts(**{name: raw[name] for name in PROMPT_VARIABLES})
