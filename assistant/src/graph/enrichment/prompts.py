"""Typed prompt set for the enrichment module.

Default templates live in `prompts/enrichment/*.md` (app root) and can be
overridden per deployment — see `prompt_store.FilePromptStore`.
"""

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel

# Allowed placeholders per template — everything the node passes at invoke
# time. `conversation` is a MessagesPlaceholder, not a template variable.
PROMPT_VARIABLES: dict[str, set[str]] = {
    "evaluate_system": {"service_context", "caller_name", "title", "description"},
    "evaluate_human": {"service_context", "caller_name", "title", "description"},
    "enrich_system": {"caller_name", "title", "description"},
    "enrich_human": {"caller_name", "title", "description"},
    "classify_service_system": {"services", "caller_name", "title", "description"},
    "classify_service_human": {"services", "caller_name", "title", "description"},
    "classify_subcategory_system": {"subcategories", "caller_name", "title", "description"},
    "classify_subcategory_human": {"subcategories", "caller_name", "title", "description"},
    "classify_ask_system": {"caller_name", "title", "description"},
    "classify_ask_human": {"caller_name", "title", "description"},
}


class EnrichmentPrompts(BaseModel):
    evaluate_system: str
    evaluate_human: str
    enrich_system: str
    enrich_human: str
    classify_service_system: str
    classify_service_human: str
    classify_subcategory_system: str
    classify_subcategory_human: str
    classify_ask_system: str
    classify_ask_human: str


def build_enrichment_prompts(raw: dict[str, str]) -> EnrichmentPrompts:
    """Validate raw templates and build the typed prompt set.

    Raises ValueError on missing templates, unparseable templates or unknown
    placeholders. Called at startup to fail fast instead of crashing on a
    live ticket; a future config UI reuses it to validate edits before saving.
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

    return EnrichmentPrompts(**{name: raw[name] for name in PROMPT_VARIABLES})
