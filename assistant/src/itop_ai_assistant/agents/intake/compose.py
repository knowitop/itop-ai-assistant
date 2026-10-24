"""Assembly for one intake run.

Everything `IntakeRun.body` (`run.py`) needs before it can construct
`IntakeAgentRun` and stream it — kept apart from the scenario itself so
"what intake does with a ticket" reads without the wiring in front of it
(TASK-026).
"""

import asyncio
import logging
from dataclasses import dataclass, replace

from langchain_core.messages import BaseMessage
from langgraph.graph.state import CompiledStateGraph

from itop_ai_assistant.config import LlmConfig
from itop_ai_assistant.domain.catalog import Service
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.ports import RunDeps
from itop_ai_assistant.repositories.sets import RepositorySet

from . import catalog
from .agent import build_intake_agent
from .config import IntakeConfig
from .context import IntakeContext
from .domain import Classification, IntakeScope, needs_classification
from .prompt import build_initial_messages, build_service_context
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
    # The administrator's switch, the deployment's ability to search and
    # whether the family it would search is indexed at all are one condition,
    # not three: `similar is not None`/`faq is not None` stay the single
    # source of all of them, checked per family. The family names come from
    # intake's own config (rule 6.5) — the subsystem is the one that knows
    # whether each is indexed. The two checks are independent, so they run
    # together rather than one after the other.
    similar_available, faq_available = await asyncio.gather(
        deps.vector_search.available(cfg.similar_family), deps.vector_search.available(cfg.faq_family)
    )
    similar = deps.vector_search if cfg.similar_enabled and similar_available else None
    faq = deps.vector_search if cfg.faq_enabled and faq_available else None
    scope = IntakeScope(
        classify=cfg.classify_enabled,
        clarify=cfg.clarify_enabled,
        handoff_note=cfg.handoff_note_enabled,
        similar=similar is not None,
        faq=faq is not None,
    )
    classification = Classification(unclassified_services=frozenset(cfg.unclassified_service_ids))
    services, service_context = await asyncio.gather(
        _offered_services(repos, cfg, ticket, scope, classification),
        build_service_context(ticket, repos.catalog_repo, classification),
    )
    # Nothing to classify into is the same condition as classification
    # switched off, folded in like `similar`/`faq`: the tools, the prompt and
    # the journal's `classify=off` all follow from it. Why it is off is a
    # deployment problem, so it goes to the log, not to the model.
    if services == []:
        logger.warning(
            f"[{run.processing_id}] {ticket.identity}: no services offered to the requester's organization, "
            "classification is off for this run — check the organization's customer contracts, "
            "the service catalogue query and the services declared not classified"
        )
        scope = replace(scope, classify=False)
        services = None
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
        faq=faq,
    )
    agent = build_intake_agent(
        deps.create_llm(llm_cfg, cfg.model),
        cfg,
        tools_for(ticket, scope, classification),
        # Nothing here can deliver prose, so force a tool call wherever the
        # endpoint accepts being forced
        force_tool_choice=llm_cfg.endpoint_forces_tool_choice,
    )
    messages = build_initial_messages(context, prompts, services, service_context)
    return ComposedRun(
        agent=agent,
        context=context,
        messages=messages,
        think_tags=tuple(llm_cfg.think_tags),
    )


async def _offered_services(
    repos: RepositorySet, cfg: IntakeConfig, ticket: Ticket, scope: IntakeScope, classification: Classification
) -> list[Service] | None:
    """The catalog for the prompt, None where the run would not classify —
    so a classified ticket costs no catalog read."""
    if not needs_classification(scope, ticket, classification):
        return None
    return await catalog.read_offered_services(repos.catalog_repo, cfg, ticket, classification)
