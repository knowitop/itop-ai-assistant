"""Selfcheck: the module that proves the seams and touches nothing.

It is the platform's own smoke test, written as an ordinary business module —
which is the point. It owns a config section, a prompt file, a schedule
trigger and a request trigger, and the core knows nothing about it beyond the
one line in `build_registry`. If a new module can be added without changing
the entry points, the registry, the journal or the UI, this file is the
evidence; if it cannot, this file breaks first.

What one run does: read the service catalog through the repository, ask the
model to say hello, write both to the run journal. No writes to iTop, no
state, no lock — nothing here is worth guarding.
"""

import logging

from langchain_core.messages import HumanMessage
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel

from itop_ai_assistant.config import ItopConfig, LlmConfig, Settings, missing_setup
from itop_ai_assistant.core.deps import create_llm
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import RunOutcome
from itop_ai_assistant.pipelines.ports import RunDeps
from itop_ai_assistant.pipelines.registry import ModuleInfo, RequestRoute, ScheduleRoute, TriggerRegistry
from itop_ai_assistant.util.text import strip_thinking

from .config import SelfCheckConfig
from .prompts import MODULE, PROMPT_VARIABLES, PROMPTS_DIR, build_selfcheck_prompts

logger = logging.getLogger(__name__)


class SelfCheckInput(BaseModel):
    """Nothing to say: the run is about the deployment, not about an object."""


def register(registry: TriggerRegistry, settings: Settings) -> None:
    cfg = settings.module_defaults(MODULE, SelfCheckConfig)
    if not cfg.enabled:
        logger.info("Selfcheck module is disabled, skipping registration")
        return

    info = ModuleInfo(
        name=MODULE,
        description="Platform smoke check: reach iTop and the model, write nothing",
        config_model=SelfCheckConfig,
        prompt_names=tuple(PROMPT_VARIABLES),
        validate_prompts=build_selfcheck_prompts,
        prompts_dir=PROMPTS_DIR,
    )
    # The same work on two triggers, which is the contract being demonstrated:
    # the clock and an operator start the identical run, and only the delivery
    # of the answer differs.
    schedules = [
        ScheduleRoute(
            name="tick",
            module=MODULE,
            handler=run_selfcheck,
            interval_of=_interval,
            default_interval=SelfCheckConfig().interval_seconds,
            subject=MODULE,
            summary="Reach iTop and the model on a timer, write nothing",
        )
    ]
    requests = [
        RequestRoute(
            action="run",
            module=MODULE,
            input_model=SelfCheckInput,
            handler=handle_request,
            subject_of=lambda _: MODULE,
            summary="Run the platform smoke check now and return the result",
        )
    ]
    registry.register(info, requests=requests, schedules=schedules)


async def _interval(deps: RunDeps) -> float:
    return (await deps.config_store.get(MODULE, SelfCheckConfig)).interval_seconds


async def handle_request(payload: SelfCheckInput, run: RunContext, deps: RunDeps) -> RunOutcome:
    return await run_selfcheck(run, deps)


async def run_selfcheck(run: RunContext, deps: RunDeps) -> RunOutcome:
    """One pass over every seam: config, iTop, prompts, model, journal."""
    cfg = await deps.config_store.get(MODULE, SelfCheckConfig)
    llm_cfg = await deps.config_store.get("llm", LlmConfig)
    missing = missing_setup(await deps.config_store.get("itop", ItopConfig), llm_cfg)
    if missing:
        reason = f"setup incomplete: {'; '.join(missing)}"
        await deps.journal.add_step(run.processing_id, "guard", reason)
        return RunOutcome(status="skipped", detail=reason)

    repos = await deps.itop.for_principal(run.principal, comment=run.comment)
    services = await repos.catalog_repo.find_services(cfg.probe_oql)
    await deps.journal.add_step(run.processing_id, "itop", f"{len(services)} services read with {cfg.probe_oql!r}")

    prompts = build_selfcheck_prompts(await deps.prompt_store.get(MODULE))
    message = PromptTemplate.from_template(prompts.greeting).format(services=len(services))
    response = await create_llm(llm_cfg, cfg.model).ainvoke([HumanMessage(content=message)])
    answer = strip_thinking(response.content, tuple(llm_cfg.think_tags)).strip()
    await deps.journal.add_step(run.processing_id, "llm", answer[:500])

    logger.info(f"[{run.processing_id}] selfcheck: {len(services)} services, model answered {len(answer)} chars")
    return RunOutcome(status="done", detail=f"{len(services)} services, model answered")
