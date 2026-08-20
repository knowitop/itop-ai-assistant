"""Typed prompt set for the selfcheck module.

Default template lives in `agents/selfcheck/prompts/greeting.md` and can be
overridden per deployment — see `prompt_store.FilePromptStore`.
"""

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from itop_ai_assistant.settings.prompt_validation import build_prompts

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


def build_selfcheck_prompts(raw: Mapping[str, str]) -> SelfCheckPrompts:
    """Validate raw templates and build the typed prompt set.

    Same contract as every module's: raises `PromptValidationError` on missing
    or unregistered templates, unparseable templates or unknown placeholders.
    Called at startup and by the admin API before saving an edit.
    """
    return build_prompts(raw, PROMPT_VARIABLES, SelfCheckPrompts, module=MODULE)
