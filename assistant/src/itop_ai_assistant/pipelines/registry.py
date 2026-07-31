"""Trigger registry: routes triggers to business-module handlers.

Two things live here on purpose, and they are not the same thing:

* `ModuleInfo` — what a **business module** is (config section, prompts, its
  screen in the admin UI). Discovery only.
* the routes — what may **start a run**. A module claims a `(class, event)`
  webhook route, a `(module, action)` request route, or both.

Keeping them apart is what lets a trigger belong to something that is not a
business module (the background sweep is infrastructure, not a module).

Adding a new module:
1. Create a package (e.g. `src/agents/<module>/`) with a `pipeline.py` exposing
   `register(registry, settings)`.
2. Subclass `shell.TicketRun` for the work itself — the lock, the object read,
   the journal steps and the guaranteed closure come from there; the module
   supplies `stop_reason` and `body`, and registers `<Run>.handle` as its route.
3. Call it from `build_registry()` below — one line.
4. Add the module's config section to `config.py`.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from itop_ai_assistant.pipelines.models import RunOutcome

if TYPE_CHECKING:
    from itop_ai_assistant.deps import AppDeps
    from itop_ai_assistant.webhook.models import WebhookPayload

logger = logging.getLogger(__name__)

# `object` as the return type, not `None`: a webhook route drops whatever the
# handler produced, and both a plain function returning None and
# `TicketRun.handle` returning a RunOutcome have to fit.
WebhookHandler = Callable[["WebhookPayload", UUID, "AppDeps"], Awaitable[object]]
RequestHandler = Callable[[Any, UUID, "AppDeps"], Awaitable[RunOutcome]]


@dataclass(frozen=True)
class ModuleInfo:
    """Metadata a business module exposes for discovery and the admin API."""

    name: str
    description: str
    config_model: type[BaseModel] | None = None
    prompt_names: tuple[str, ...] = ()
    # Validates a full {name: template} set; raises ValueError on bad templates.
    # Used at startup and by the admin API before saving prompt edits.
    validate_prompts: Callable[[dict[str, str]], object] | None = None


@dataclass(frozen=True)
class RequestRoute:
    """A synchronous entry point into a module: one call, one answer.

    `input_model` is what the caller must send (its JSON schema is what the
    admin UI builds its form from); `subject_of` names the object the run is
    about, for the journal.
    """

    action: str
    module: str
    input_model: type[BaseModel]
    handler: RequestHandler
    subject_of: Callable[[Any], str]
    summary: str = ""


class TriggerRegistry:
    """What may start a run: webhook events and synchronous requests.

    Modules claim their triggers at startup; an entry point rejects anything no
    module has claimed.
    """

    def __init__(self) -> None:
        self._webhooks: dict[tuple[str, str], tuple[str, WebhookHandler]] = {}
        self._requests: dict[tuple[str, str], RequestRoute] = {}
        self._modules: dict[str, ModuleInfo] = {}

    def register(
        self,
        module: ModuleInfo,
        *,
        webhooks: Mapping[tuple[str, str], WebhookHandler] | None = None,
        requests: Iterable[RequestRoute] = (),
    ) -> None:
        if module.name in self._modules:
            raise ValueError(f"Module {module.name!r} is already registered")
        webhooks = webhooks or {}
        conflicts = webhooks.keys() & self._webhooks.keys()
        if conflicts:
            raise ValueError(f"Webhook routes already claimed by another module: {sorted(conflicts)}")
        requests = list(requests)
        for route in requests:
            if (route.module, route.action) in self._requests:
                raise ValueError(f"Request route {route.module}/{route.action} is already claimed")
        self._modules[module.name] = module
        self._webhooks.update({key: (module.name, handler) for key, handler in webhooks.items()})
        self._requests.update({(route.module, route.action): route for route in requests})
        logger.info(
            f"Registered module {module.name!r} with {len(webhooks)} webhook routes and {len(requests)} request routes"
        )

    def resolve_webhook(self, obj_class: str, event: str) -> tuple[str, WebhookHandler] | None:
        """Return (module name, handler) for a webhook route, or None."""
        return self._webhooks.get((obj_class, str(event)))

    def resolve_request(self, module: str, action: str) -> RequestRoute | None:
        return self._requests.get((module, action))

    def requests_for(self, module: str) -> list[RequestRoute]:
        return [route for (owner, _), route in self._requests.items() if owner == module]

    def get_module(self, name: str) -> ModuleInfo | None:
        return self._modules.get(name)

    @property
    def modules(self) -> list[ModuleInfo]:
        return list(self._modules.values())


def build_registry(settings) -> "TriggerRegistry":
    """Assemble the registry from all known modules. New module = one line here."""
    from itop_ai_assistant.agents.intake import pipeline as intake_pipeline

    registry = TriggerRegistry()
    intake_pipeline.register(registry, settings)
    return registry
