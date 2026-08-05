"""Intake module: a ticket in, one tool-calling agent over it.

Deterministic shell, agentic core — the shell itself is the core's
(`pipelines/shell.py`, `pipelines/agent_run.py`). What this module supplies is
intake's own: the guard's three predicates, the body that assembles the agent,
and what to write when the agent closed nothing itself.
"""

import logging

from itop_ai_assistant.config import EmbeddingsConfig, IntakeConfig, LlmConfig, Settings, VectorConfig
from itop_ai_assistant.deps import AppDeps, create_llm
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.agent_run import AgentRun
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import ObjectRef
from itop_ai_assistant.pipelines.registry import ModuleInfo, RequestRoute, TriggerRegistry
from itop_ai_assistant.pipelines.shell import TicketRun
from itop_ai_assistant.vector.embedder import EmbeddingsClient
from itop_ai_assistant.vector.search import SimilarSearch
from itop_ai_assistant.webhook.models import TicketEvent

from .agent import TERMINAL_TOOLS, build_intake_agent
from .context import IntakeContext
from .prompt import build_initial_messages
from .prompts import PROMPT_VARIABLES, build_intake_prompts
from .tools import tools_for

logger = logging.getLogger(__name__)


def register(registry: TriggerRegistry, settings: Settings) -> None:
    cfg = settings.intake
    if not cfg.enabled:
        logger.info("Intake module is disabled, skipping registration")
        return

    info = ModuleInfo(
        name="intake",
        description="Agentic (tool-calling) ticket intake: classify, ask, hand off",
        config_model=IntakeConfig,
        prompt_names=tuple(PROMPT_VARIABLES),
        validate_prompts=build_intake_prompts,
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
            input_model=ObjectRef,
            handler=IntakeRun.handle,
            subject_of=lambda ref: ref.label,
            summary="Run intake on one ticket now and return the outcome",
        )
    ]
    registry.register(info, webhooks=webhooks, requests=requests)


class IntakeRun(TicketRun):
    """Ticket created or user commented: classify, ask, hand off."""

    async def stop_reason(self, ticket: Ticket, ai_name: str) -> str | None:
        """Why this ticket must not be processed, or None to proceed."""
        ticket_state = await self.deps.state_manager.get(ticket.label)
        if ticket_state.ai_done:
            return "already processed (ai_done)"

        active_statuses = self.bundle.ticket_repo.mapping.active_statuses
        if ticket.status not in active_statuses:
            return f"status={ticket.status} not in {active_statuses}"

        # Loop protection, second line of defense after iTop trigger contexts:
        # if our own question is the last public entry, wait for the user instead
        # of reacting to our own comment or a duplicate webhook.
        if ticket.public_log and ticket.public_log[-1].user_login == ai_name:
            return "last public entry is ours, waiting for the requester"

        return None

    async def body(self, ticket: Ticket, ai_name: str) -> None:
        logger.info(f"[{self.processing_id}] Running intake agent for {ticket.label}")

        cfg = await self.deps.config_store.get("intake", IntakeConfig)
        llm_cfg = await self.deps.config_store.get("llm", LlmConfig)
        prompts = build_intake_prompts(await self.deps.prompt_store.get("intake"))
        vector_cfg, embedder = await self._similar_search_parts()
        similar = (
            SimilarSearch(self.deps.vector_store, embedder, self.bundle.ticket_repo.find_existing_ids)
            if embedder is not None
            else None
        )
        context = IntakeContext(
            processing_id=self.processing_id,
            ticket=ticket,
            ticket_repo=self.bundle.ticket_repo,
            catalog_repo=self.bundle.catalog_repo,
            state_manager=self.deps.state_manager,
            intake=cfg,
            ai_name=ai_name,
            similar=similar,
            vector=vector_cfg,
        )
        agent = build_intake_agent(
            create_llm(llm_cfg, cfg.model),
            cfg,
            tools_for(ticket, similar=similar is not None),
            # Nothing here can deliver prose, so force a tool call wherever the
            # endpoint accepts being forced
            force_tool_choice=llm_cfg.endpoint_forces_tool_choice,
        )
        messages = await build_initial_messages(context, prompts)

        try:
            await IntakeAgentRun(
                agent,
                context,
                deps=self.deps,
                run=self.run,
                think_tags=tuple(llm_cfg.think_tags),
            ).stream(messages)
        finally:
            if embedder is not None:
                await embedder.aclose()

    async def _similar_search_parts(self) -> tuple[VectorConfig | None, EmbeddingsClient | None]:
        """The vector config and an embeddings client, or (config, None) when
        this deployment cannot search.

        Three conditions, all necessary: a vector store to search, indexing
        switched on — a stale index quoted as current is worse than no
        references at all — and an embeddings endpoint to turn the ticket into
        a query with. The client is per run and closed by the caller.
        """
        if not self.deps.vector_store.configured:
            return None, None
        vector_cfg = await self.deps.config_store.get("vector", VectorConfig)
        emb_cfg = await self.deps.config_store.get("embeddings", EmbeddingsConfig)
        if not vector_cfg.enabled or not emb_cfg.base_url or not emb_cfg.model:
            return vector_cfg, None
        return vector_cfg, EmbeddingsClient(emb_cfg)


class IntakeAgentRun(AgentRun[IntakeContext]):
    """The intake agent loop: one question or one handoff, never both."""

    terminal_tools = TERMINAL_TOOLS

    async def epilogue(self) -> None:
        """Close a run the agent did not close itself.

        The model answered in plain text, burned `max_iterations` or looped.
        Setting `ai_done` is mandatory: otherwise the next webhook replays the
        whole expensive cycle to the same end.

        The `ai_done` re-read guards a real race: `handle_assigned` takes no
        lock, so an engineer can pick the ticket up while the agent is still
        thinking — and a fallback note posted after that would land on someone
        else's ticket.
        """
        ticket = self.context.ticket
        state = await self.deps.state_manager.get(ticket.label)
        if state.ai_done:
            await self.step("epilogue", "ticket already finished — nothing to close")
            return

        logger.info(f"[{self.processing_id}] {ticket.label}: agent produced no terminal action, posting fallback note")
        await self.context.ticket_repo.append_private_log(ticket, self.context.intake.handoff_fallback_note)
        await self.deps.state_manager.mark_done(ticket.label)
        await self.step("epilogue", "no terminal action — fallback note posted, ai_done set")


async def handle_assigned(ref: ObjectRef, run: RunContext, deps: AppDeps) -> None:
    """Engineer took the ticket: stop any further AI processing.

    Not a `TicketRun`: no lock and no read. The missing lock is deliberate — it
    is what lets this land while an agent is mid-run, and what
    `IntakeAgentRun.epilogue` re-reads `ai_done` for.
    """
    await deps.state_manager.mark_done(ref.label)
    logger.info(f"[{run.processing_id}] {ref.label} assigned, marked done")
