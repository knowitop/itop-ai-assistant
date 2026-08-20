"""Placeholder validation, shared by every module's prompt set.

A module declares its templates and their allowed placeholders in its own
`PROMPT_VARIABLES` and builds its typed set through `build_prompts`; what
counts as a broken template is decided here, once, for all of them.

Errors are addressed by template name, because that is the granularity both
callers work at: the admin UI marks the individual prompt, and the startup
check routes on it — a template the deployment overrode only warns, one of
ours refuses the boot (REQ-005).
"""

from collections.abc import Mapping

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel


class PromptValidationError(ValueError):
    """Broken prompt templates of one module, keyed by template name.

    A `ValueError` so the admin API keeps answering 422 on a rejected edit.
    The class name is also what makes the journal entry of a run that died on
    a template read as a template problem rather than an iTop or model failure.
    """

    def __init__(self, module: str, errors: Mapping[str, str]) -> None:
        self.module = module
        self.errors = dict(errors)
        detail = "\n".join(f"{module}/{name}: {message}" for name, message in sorted(self.errors.items()))
        super().__init__(f"Invalid prompt templates:\n{detail}")


def check_templates(raw: Mapping[str, str], variables: Mapping[str, set[str]]) -> dict[str, str]:
    """Template name -> what is wrong with it; empty when the set is sound.

    A template nobody registered counts as broken: the admin API offers it for
    editing and it then never reaches the model.
    """
    errors = {name: "template is missing" for name in variables.keys() - raw.keys()}
    errors.update(
        {name: f"template is not registered, known: {sorted(variables)}" for name in raw.keys() - variables.keys()}
    )
    for name, allowed in variables.items():
        if name not in raw:
            continue
        try:
            used = set(PromptTemplate.from_template(raw[name]).input_variables)
        except ValueError as e:
            errors[name] = f"cannot parse template: {e}"
            continue
        unknown = used - allowed
        if unknown:
            errors[name] = f"unknown placeholders {sorted(unknown)}, allowed: {sorted(allowed)}"
    return errors


def build_prompts[T: BaseModel](
    raw: Mapping[str, str], variables: Mapping[str, set[str]], model: type[T], *, module: str
) -> T:
    """Validate raw templates and build the module's typed prompt set."""
    errors = check_templates(raw, variables)
    if errors:
        raise PromptValidationError(module, errors)
    return model(**{name: raw[name] for name in variables})
