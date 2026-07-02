"""Pipeline registry: routes webhook events to business-module handlers.

Adding a new module:
1. Create a package (e.g. `src/graph/<module>/`) with a `pipeline.py` exposing
   `register(registry, settings)`.
2. Call it from `build_registry()` below — one line.
3. Add the module's config section to `config.py`.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from deps import AppDeps
    from webhook.models import WebhookPayload

logger = logging.getLogger(__name__)

PipelineHandler = Callable[["WebhookPayload", UUID, "AppDeps"], Awaitable[None]]


@dataclass(frozen=True)
class ModuleInfo:
    """Metadata a business module exposes for discovery (and a future admin UI)."""

    name: str
    description: str
    config_model: type | None = None
    prompt_names: tuple[str, ...] = ()


class PipelineRegistry:
    """Maps (object class, event) to a module handler.

    Modules claim their routes at startup; the webhook router rejects any
    (class, event) combination no module has claimed.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], PipelineHandler] = {}
        self._modules: dict[str, ModuleInfo] = {}

    def register(self, module: ModuleInfo, routes: dict[tuple[str, str], PipelineHandler]) -> None:
        if module.name in self._modules:
            raise ValueError(f"Module {module.name!r} is already registered")
        conflicts = routes.keys() & self._routes.keys()
        if conflicts:
            raise ValueError(f"Routes already claimed by another module: {sorted(conflicts)}")
        self._modules[module.name] = module
        self._routes.update(routes)
        logger.info(f"Registered module {module.name!r} with {len(routes)} routes")

    def resolve(self, obj_class: str, event: str) -> PipelineHandler | None:
        return self._routes.get((obj_class, str(event)))

    @property
    def modules(self) -> list[ModuleInfo]:
        return list(self._modules.values())


def build_registry(settings) -> "PipelineRegistry":
    """Assemble the registry from all known modules. New module = one line here."""
    from graph.enrichment import pipeline as enrichment_pipeline

    registry = PipelineRegistry()
    enrichment_pipeline.register(registry, settings)
    return registry
