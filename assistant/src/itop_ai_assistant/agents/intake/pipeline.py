"""Intake module: webhook events → one tool-calling agent.

Deterministic shell, agentic core — the shell itself is the core's
(`pipelines/shell.py`, `pipelines/agent_run.py`). What this module supplies is
intake's own: the guard's three predicates, the body that assembles the agent,
and what to write when the agent closed nothing itself.
"""

import logging
from uuid import UUID

from itop_ai_assistant.config import IntakeConfig, LlmConfig, Settings
from itop_ai_assistant.deps import AppDeps, create_llm
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.agent_run import AgentRun
from itop_ai_assistant.pipelines.registry import ModuleInfo, PipelineRegistry
from itop_ai_assistant.pipelines.shell import TicketRun
from itop_ai_assistant.webhook.models import TicketEvent, WebhookPayload

from .agent import TERMINAL_TOOLS, build_intake_agent
from .context import IntakeContext
from .prompt import build_initial_messages
from .prompts import PROMPT_VARIABLES, build_intake_prompts
from .tools import tools_for

logger = logging.getLogger(__name__)


def register(registry: PipelineRegistry, settings: Settings) -> None:
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
    routes = {}
    for obj_class in cfg.classes:
        for event in (TicketEvent.CREATED, TicketEvent.USER_COMMENTED, TicketEvent.ASSIGNED):
            routes[(obj_class, str(event))] = handle_assigned if event is TicketEvent.ASSIGNED else IntakeRun.handle
    registry.register(info, routes)


class IntakeRun(TicketRun):
    """Ticket created or user commented: classify, ask, hand off."""

    async def stop_reason(self, ticket: Ticket, ai_name: str) -> str | None:
        """Why this ticket must not be processed, or None to proceed."""
        ticket_state = await self.deps.state_manager.get(ticket.label)
        if ticket_state.ai_done:
            return "already processed (ai_done) — skipped"

        active_statuses = self.bundle.ticket_repo.mapping.active_statuses
        if ticket.status not in active_statuses:
            return f"status={ticket.status} not in {active_statuses} — skipped"

        # Loop protection, second line of defense after iTop trigger contexts:
        # if our own question is the last public entry, wait for the user instead
        # of reacting to our own comment or a duplicate webhook.
        if ticket.public_log and ticket.public_log[-1].user_login == ai_name:
            return "last public entry is ours, waiting for the requester — skipped"

        return None

    async def body(self, ticket: Ticket, ai_name: str) -> None:
        logger.info(f"[{self.processing_id}] Running intake agent for {ticket.label}")

        cfg = await self.deps.config_store.get("intake", IntakeConfig)
        llm_cfg = await self.deps.config_store.get("llm", LlmConfig)
        prompts = build_intake_prompts(await self.deps.prompt_store.get("intake"))
        context = IntakeContext(
            processing_id=self.processing_id,
            ticket=ticket,
            ticket_repo=self.bundle.ticket_repo,
            catalog_repo=self.bundle.catalog_repo,
            state_manager=self.deps.state_manager,
            intake=cfg,
            ai_name=ai_name,
        )
        agent = build_intake_agent(
            create_llm(llm_cfg, cfg.model),
            cfg,
            tools_for(ticket),
            # Nothing here can deliver prose, so force a tool call wherever the
            # endpoint accepts being forced
            force_tool_choice=llm_cfg.endpoint_forces_tool_choice,
        )
        messages = await build_initial_messages(context, prompts)

        await IntakeAgentRun(
            agent,
            context,
            deps=self.deps,
            processing_id=self.processing_id,
            think_tags=tuple(llm_cfg.think_tags),
        ).stream(messages)


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


async def handle_assigned(payload: WebhookPayload, processing_id: UUID, deps: AppDeps) -> None:
    """Engineer took the ticket: stop any further AI processing.

    Not a `TicketRun`: no lock and no read. The missing lock is deliberate — it
    is what lets this land while an agent is mid-run, and what
    `IntakeAgentRun.epilogue` re-reads `ai_done` for.
    """
    label = f"{payload.obj_class}::{payload.id}"
    await deps.state_manager.mark_done(label)
    logger.info(f"[{processing_id}] {label} assigned, marked done")
