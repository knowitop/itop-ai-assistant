"""Intake module registration: routes an object gets a run for, nothing else.

Assembly (`compose.py`) and the scenario (`run.py`) live apart from this file
on purpose (TASK-026) — this is only ever the composition-root wiring every
module exposes the same way (`<module>/pipeline.py::register`,
`pipelines/registry.py::build_registry`).
"""

import logging
from pathlib import Path

from itop_ai_assistant.config import Settings
from itop_ai_assistant.domain.identity import ObjectIdentity
from itop_ai_assistant.pipelines.models import TicketEvent
from itop_ai_assistant.pipelines.registry import ModuleInfo, RequestRoute, TriggerRegistry

from .config import IntakeConfig
from .prompts import MODULE, PROMPT_VARIABLES, PROMPTS_DIR, build_intake_prompts
from .run import IntakeRun, handle_assigned

logger = logging.getLogger(__name__)

# What the admin UI shows about this module, in the languages we ship
# (ADR-030). English is not a file here — it is the config model's own
# `title`/`description`.
LOCALES_DIR = Path(__file__).parent / "locales"


def register(registry: TriggerRegistry, settings: Settings) -> None:
    cfg = settings.module_defaults(MODULE, IntakeConfig)
    if not cfg.enabled:
        logger.info("Intake module is disabled, skipping registration")
        return

    info = ModuleInfo(
        name=MODULE,
        description="Agentic (tool-calling) ticket intake: classify, ask, hand off",
        config_model=IntakeConfig,
        prompt_names=tuple(PROMPT_VARIABLES),
        validate_prompts=build_intake_prompts,
        prompts_dir=PROMPTS_DIR,
        locales_dir=LOCALES_DIR,
    )
    webhooks = {}
    for obj_class in cfg.classes:
        for event in (TicketEvent.CREATED, TicketEvent.USER_COMMENTED, TicketEvent.ASSIGNED):
            webhooks[(obj_class, str(event))] = handle_assigned if event is TicketEvent.ASSIGNED else IntakeRun.handle
    # The same run, triggered by hand: the caller waits and gets the outcome
    # instead of it going into the ticket only. The guard is not bypassed — a
    # ticket the assistant is done with answers "skipped", which is the truth.
    requests = [
        RequestRoute(
            action="process",
            module=info.name,
            input_model=ObjectIdentity,
            handler=IntakeRun.handle,
            subject_of=lambda ref: str(ref),
            summary="Run intake on one ticket now and return the outcome",
        )
    ]
    registry.register(info, webhooks=webhooks, requests=requests)
