"""Assembling one day's document — the only place the four groups meet.

Nothing here decides whether the document is sent, or when: `build()` answers
"what would go out for this day", and the caller decides what to do with it.
That is what lets the preview (REQ-009 R7) show the real thing rather than a
description of it — it calls the same method the sender calls.

The configuration group is the part worth reading twice. It is not a list of
fields somebody remembered to add: every module's own config section is
scanned, and the fields whose declared type is `bool` or `int` travel, under
`<section>_<field>`. R4 is satisfied by construction rather than by
discipline — a module's OQL, its class list, its fallback note text cannot
appear in the document, because they are neither. "Which modules are on" and
"the question budget" from R3 fall out of the same rule as `intake_enabled`
and `intake_max_questions`, and a module written next year describes itself
here without a line changed.

Redis failures are not caught. Without Redis there is no install id and no
counters, so there is no document — and R5 forbids giving the sender a way
around that: a cached document or in-memory counters would hide the failure
and cost nothing to whoever added them.
"""

import logging
from datetime import UTC, date, datetime

from pydantic import BaseModel

from itop_ai_assistant.config import LlmConfig, PlatformConfig, Settings
from itop_ai_assistant.pipelines.registry import TriggerRegistry
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.state.counters import DailyCounters
from itop_ai_assistant.telemetry import normalize
from itop_ai_assistant.telemetry.document import Build, Configuration, Environment, TelemetryDocument
from itop_ai_assistant.telemetry.install import InstallIdentity
from itop_ai_assistant.util.build_info import get_build_info
from itop_ai_assistant.vector import SimilarSearch, VectorConfig

logger = logging.getLogger(__name__)

#: Sections scanned for the configuration group beyond the business modules.
#: `vector` is infrastructure rather than a module, but it is configuration an
#: administrator sets and it answers "is the layer worth its complexity",
#: which is one of the questions the requirement exists to ask.
_EXTRA_SECTIONS: dict[str, type[BaseModel]] = {"vector": VectorConfig}


class DocumentBuilder:
    """Reads the installation and hands back one day's document."""

    def __init__(
        self,
        settings: Settings,
        config_store: ConfigStore,
        registry: TriggerRegistry,
        counters: DailyCounters,
        install: InstallIdentity,
        vector_search: SimilarSearch,
    ):
        self._settings = settings
        self._config = config_store
        self._registry = registry
        self._counters = counters
        self._install = install
        self._vector_search = vector_search

    async def build(self, day: date | None = None) -> TelemetryDocument:
        """The document for `day`, today in UTC by default.

        Which day is actually sent is the sender's business; the preview asks
        for today's, since today's is what would leave today.
        """
        day = day or datetime.now(UTC).date()
        build = get_build_info()
        llm = await self._config.get("llm", LlmConfig)
        platform = await self._config.get("platform", PlatformConfig)
        counters = await self._counters.read(day)
        return TelemetryDocument(
            install_id=await self._install.install_id(),
            day=day,
            build=Build(
                version=build.version,
                commit=build.commit,
                python_version=normalize.python_version(),
                containerized=normalize.in_container(),
            ),
            environment=Environment(
                qdrant=self._settings.qdrant_url is not None,
                vector_available=await self._vector_search.available(),
                admin_language=await self._install.language(),
                utc_offset_minutes=normalize.utc_offset_minutes(),
            ),
            configuration=Configuration(
                dry_run=platform.dry_run,
                llm_provider=normalize.llm_provider(llm.provider),
                llm_model=normalize.model_name(llm.model),
                settings=await self._module_settings(),
            ),
            activity={counter.value: amount for counter, amount in counters.items()},
        )

    async def _module_settings(self) -> dict[str, bool | int]:
        """Every `bool` and `int` every configurable section declares.

        The declared type is what decides, not the value: a field annotated
        `str | None` that happens to be unset stays out, so a section cannot
        start leaking the day somebody fills it in.
        """
        sections: dict[str, type[BaseModel]] = {
            module.name: module.config_model for module in self._registry.modules if module.config_model is not None
        }
        sections.update(_EXTRA_SECTIONS)

        values: dict[str, bool | int] = {}
        for name, model in sorted(sections.items()):
            cfg = await self._config.get(name, model)
            for field, info in type(cfg).model_fields.items():
                if info.annotation in (bool, int):
                    values[f"{name}_{field}"] = getattr(cfg, field)
        return values
