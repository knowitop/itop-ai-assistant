"""Assembly for one intake run.

Everything `IntakeRun.body` (`run.py`) needs before it can construct
`IntakeAgentRun` and stream it — kept apart from the scenario itself so
"what intake does with a ticket" reads without the wiring in front of it
(TASK-026).
"""

import logging
from dataclasses import dataclass

from langchain_core.messages import BaseMessage
from langgraph.graph.state import CompiledStateGraph

from itop_ai_assistant.config import LlmConfig
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.ports import RunDeps
from itop_ai_assistant.repositories.sets import RepositorySet

from .agent import build_intake_agent
from .config import IntakeConfig
from .context import IntakeContext
from .domain import Classification, IntakeScope
from .prompt import build_initial_messages
from .prompts import MODULE, build_intake_prompts
from .state import IntakeState
from .tools import tools_for

logger = logging.getLogger(__name__)


@dataclass
class ComposedRun:
    """What `body()` needs to construct `IntakeAgentRun` and stream it."""

    agent: CompiledStateGraph
    context: IntakeContext
    messages: list[BaseMessage]
    think_tags: tuple[str, ...]


async def assemble(ticket: Ticket, ai_name: str, run: RunContext, deps: RunDeps, repos: RepositorySet) -> ComposedRun:
    cfg = await deps.config_store.get(MODULE, IntakeConfig)
    llm_cfg = await deps.config_store.get("llm", LlmConfig)
    prompts = build_intake_prompts((await deps.prompt_store.get(MODULE)).effective)
    # The administrator's switch and the deployment's ability to search are one
    # condition, not two: `similar is not None` stays the single source of both
    similar = deps.vector_search if cfg.similar_enabled and await deps.vector_search.available() else None
    scope = IntakeScope(
        classify=cfg.classify_enabled,
        clarify=cfg.clarify_enabled,
        handoff_note=cfg.handoff_note_enabled,
        similar=similar is not None,
    )
    classification = Classification(unclassified_services=frozenset(cfg.unclassified_service_ids))
    context = IntakeContext(
        processing_id=run.processing_id,
        principal=run.principal,
        ticket=ticket,
        ticket_repo=repos.ticket_repo,
        catalog_repo=repos.catalog_repo,
        state_manager=IntakeState(deps.state_manager),
        intake=cfg,
        scope=scope,
        classification=classification,
        ai_name=ai_name,
        similar=similar,
    )
    agent = build_intake_agent(
        deps.create_llm(llm_cfg, cfg.model),
        cfg,
        tools_for(ticket, scope, classification),
        # Nothing here can deliver prose, so force a tool call wherever the
        # endpoint accepts being forced
        force_tool_choice=llm_cfg.endpoint_forces_tool_choice,
    )
    messages = await build_initial_messages(context, prompts)
    return ComposedRun(
        agent=agent,
        context=context,
        messages=messages,
        think_tags=tuple(llm_cfg.think_tags),
    )
