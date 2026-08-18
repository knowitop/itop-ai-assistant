"""Trigger registry: routes triggers to business-module handlers.

Two things live here on purpose, and they are not the same thing:

* `ModuleInfo` — what a **business module** is (config section, prompts, its
  screen in the admin UI). Discovery only.
* the routes — what may **start a run**. A module claims a `(class, event)`
  webhook route, a `(module, action)` request route, a `(module, name)`
  schedule route, or any combination.

Keeping them apart is what lets one module own several triggers, and one
trigger be claimed without the module owning the others.

What is *not* here: a background loop that starts no run. The vector sweep
ticks forever and journals into Redis, but it has no module, no subject and
no principal — it takes pacing from `pipelines/scheduler.py` and nothing else.
"Periodic" and "a trigger of a business module" are different questions, and a
route answers only the second one.

Adding a new module:
1. Create a package (e.g. `src/agents/<module>/`) with a `pipeline.py` exposing
   `register(registry, settings)`.
2. Subclass `shell.TicketRun` for the work itself — the lock, the object read,
   the journal steps and the guaranteed closure come from there; the module
   supplies `stop_reason` and `body`, and registers `<Run>.handle` as its route.
3. Call it from `build_registry()` below — one line.
4. If it owns config, define the model in its own package
   (`agents/<module>/config.py`) and read defaults with
   `settings.module_defaults(name, Model)` — nothing to add to `config.py`.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import RunOutcome
from itop_ai_assistant.pipelines.ports import RunDeps

if TYPE_CHECKING:
    from itop_ai_assistant.webhook.models import WebhookPayload

logger = logging.getLogger(__name__)

# Handlers are typed by the `RunDeps` port, not by the container that satisfies
# it: the registry describes what a module may be handed, and a module has no
# business owning the connection pool or reading startup settings.
#
# `object` as the return type, not `None`: a webhook route drops whatever the
# handler produced, and both a plain function returning None and
# `TicketRun.handle` returning a RunOutcome have to fit.
WebhookHandler = Callable[["WebhookPayload", RunContext, RunDeps], Awaitable[object]]
RequestHandler = Callable[[Any, RunContext, RunDeps], Awaitable[RunOutcome]]
# No payload: the clock carries no information beyond "it is time"
ScheduleHandler = Callable[[RunContext, RunDeps], Awaitable[RunOutcome]]


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
    # Not every module has prompt text, symmetric with `config_model`.
    # `FilePromptStore` skips a module for which this is unset when it builds
    # its map of default directories.
    prompts_dir: Path | None = None


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


@dataclass(frozen=True)
class ScheduleRoute:
    """A periodic entry point into a module: the clock starts the run.

    `interval_of` re-reads the period from the module's runtime config on every
    tick, so an edit in the admin UI applies without a restart;
    `default_interval` is what the loop falls back to when that read fails.
    `subject` names what the run is about for the journal — a schedule has no
    payload to derive it from, so it is a constant (and, unlike a webhook's,
    usually not a ticket).
    """

    name: str
    module: str
    handler: ScheduleHandler
    interval_of: Callable[[RunDeps], Awaitable[float]]
    default_interval: float = 300.0
    subject: str = ""
    summary: str = ""

    @property
    def label(self) -> str:
        return f"{self.module}/{self.name}"


class TriggerRegistry:
    """What may start a run: webhook events, synchronous requests, schedules.

    Modules claim their triggers at startup; an entry point rejects anything no
    module has claimed.
    """

    def __init__(self) -> None:
        self._webhooks: dict[tuple[str, str], tuple[str, WebhookHandler]] = {}
        self._requests: dict[tuple[str, str], RequestRoute] = {}
        self._schedules: dict[tuple[str, str], ScheduleRoute] = {}
        self._modules: dict[str, ModuleInfo] = {}

    def register(
        self,
        module: ModuleInfo,
        *,
        webhooks: Mapping[tuple[str, str], WebhookHandler] | None = None,
        requests: Iterable[RequestRoute] = (),
        schedules: Iterable[ScheduleRoute] = (),
    ) -> None:
        if module.name in self._modules:
            raise ValueError(f"Module {module.name!r} is already registered")
        webhooks = webhooks or {}
        conflicts = webhooks.keys() & self._webhooks.keys()
        if conflicts:
            raise ValueError(f"Webhook routes already claimed by another module: {sorted(conflicts)}")
        requests = list(requests)
        for request_route in requests:
            if (request_route.module, request_route.action) in self._requests:
                raise ValueError(f"Request route {request_route.module}/{request_route.action} is already claimed")
        schedules = list(schedules)
        for schedule_route in schedules:
            if (schedule_route.module, schedule_route.name) in self._schedules:
                raise ValueError(f"Schedule route {schedule_route.label} is already claimed")
        self._modules[module.name] = module
        self._webhooks.update({key: (module.name, handler) for key, handler in webhooks.items()})
        self._requests.update({(route.module, route.action): route for route in requests})
        self._schedules.update({(route.module, route.name): route for route in schedules})
        logger.info(
            f"Registered module {module.name!r} with {len(webhooks)} webhook routes, "
            f"{len(requests)} request routes and {len(schedules)} schedules"
        )

    def resolve_webhook(self, obj_class: str, event: str) -> tuple[str, WebhookHandler] | None:
        """Return (module name, handler) for a webhook route, or None."""
        return self._webhooks.get((obj_class, str(event)))

    def resolve_request(self, module: str, action: str) -> RequestRoute | None:
        return self._requests.get((module, action))

    def resolve_schedule(self, module: str, name: str) -> ScheduleRoute | None:
        return self._schedules.get((module, name))

    def requests_for(self, module: str) -> list[RequestRoute]:
        return [route for (owner, _), route in self._requests.items() if owner == module]

    def schedules_for(self, module: str) -> list[ScheduleRoute]:
        return [route for (owner, _), route in self._schedules.items() if owner == module]

    @property
    def schedules(self) -> list[ScheduleRoute]:
        return list(self._schedules.values())

    def get_module(self, name: str) -> ModuleInfo | None:
        return self._modules.get(name)

    @property
    def modules(self) -> list[ModuleInfo]:
        return list(self._modules.values())


def build_registry(settings) -> "TriggerRegistry":
    """Assemble the registry from all known modules. New module = one line here."""
    from itop_ai_assistant.agents.intake import pipeline as intake_pipeline
    from itop_ai_assistant.agents.selfcheck import pipeline as selfcheck_pipeline

    registry = TriggerRegistry()
    intake_pipeline.register(registry, settings)
    selfcheck_pipeline.register(registry, settings)
    return registry
