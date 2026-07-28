"""Intake module: webhook events → one tool-calling agent.

The agentic counterpart of `graph/enrichment` — same job (classify, ask,
hand off), different machinery. Deterministic shell, agentic core: the lock,
the guard and the epilogue are plain code here; everything between them is
the agent's decision.

Deliberately duplicates the enrichment shell instead of sharing it: the two
modules are an A/B experiment and the loser gets deleted whole.
"""

import logging
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from langchain_core.messages import AIMessage, ToolMessage

from config import IntakeConfig, LlmConfig, Settings
from deps import AppDeps, ItopBundle, create_llm
from domain.ticket import Ticket
from pipelines.registry import ModuleInfo, PipelineRegistry
from webhook.models import TicketEvent, WebhookPayload

from .agent import build_intake_agent, describe_ai_message, is_terminal_result
from .context import IntakeContext
from .prompt import build_initial_messages
from .prompts import PROMPT_VARIABLES, build_intake_prompts

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
            # A/B runs split classes between the modules; an overlap is a
            # misconfiguration, not a reason to refuse to boot
            if registry.resolve(obj_class, str(event)) is not None:
                logger.warning(f"intake: {obj_class}/{event} is already claimed by another module, skipping")
                continue
            routes[(obj_class, str(event))] = handle_assigned if event is TicketEvent.ASSIGNED else handle_ticket_event
    registry.register(info, routes)


async def handle_ticket_event(payload: WebhookPayload, processing_id: UUID, deps: AppDeps) -> None:
    """Ticket created or user commented: run the intake agent under a per-ticket lock."""
    label = f"{payload.obj_class}::{payload.id}"

    if not await deps.state_manager.acquire_lock(label):
        logger.info(f"[{processing_id}] {label} is already being processed, skipping")
        await deps.journal.add_step(processing_id, "lock", "ticket is already being processed — skipped")
        return
    try:
        bundle = await deps.itop.get()
        ticket = await bundle.ticket_repo.fetch(payload.obj_class, payload.id)
        if ticket is None:
            logger.warning(f"[{processing_id}] {label} not found in iTop, skipping")
            await deps.journal.add_step(processing_id, "fetch", "ticket not found in iTop — skipped")
            return
        ai_name = await bundle.ticket_repo.get_ai_person_name()
        # The guard runs before the agent, not as middleware: it needs no LLM
        # and it saves the catalog round-trip to iTop on a no-op webhook
        reason = await _stop_reason(ticket, ai_name, bundle, deps)
        if reason:
            logger.info(f"[{processing_id}] {label}: {reason}")
            await deps.journal.add_step(processing_id, "guard", reason)
            return
        await _run_intake_agent(ticket, ai_name, bundle, processing_id, deps)
    finally:
        await deps.state_manager.release_lock(label)


async def handle_assigned(payload: WebhookPayload, processing_id: UUID, deps: AppDeps) -> None:
    """Engineer took the ticket: stop any further AI processing."""
    label = f"{payload.obj_class}::{payload.id}"
    await deps.state_manager.mark_done(label)
    logger.info(f"[{processing_id}] {label} assigned, marked done")


async def _stop_reason(ticket: Ticket, ai_name: str, bundle: ItopBundle, deps: AppDeps) -> str | None:
    """Why this ticket must not be processed, or None to proceed."""
    ticket_state = await deps.state_manager.get(ticket.label)
    if ticket_state.ai_done:
        return "already processed (ai_done) — skipped"

    active_statuses = bundle.ticket_repo.mapping.active_statuses
    if ticket.status not in active_statuses:
        return f"status={ticket.status} not in {active_statuses} — skipped"

    # Loop protection, second line of defense after iTop trigger contexts:
    # if our own question is the last public entry, wait for the user instead
    # of reacting to our own comment or a duplicate webhook.
    if ticket.public_log and ticket.public_log[-1].user_login == ai_name:
        return "last public entry is ours, waiting for the requester — skipped"

    return None


async def _run_intake_agent(
    ticket: Ticket, ai_name: str, bundle: ItopBundle, processing_id: UUID, deps: AppDeps
) -> None:
    logger.info(f"[{processing_id}] Running intake agent for {ticket.label}")

    cfg = await deps.config_store.get("intake", IntakeConfig)
    llm_cfg = await deps.config_store.get("llm", LlmConfig)
    prompts = build_intake_prompts(await deps.prompt_store.get("intake"))
    context = IntakeContext(
        processing_id=processing_id,
        ticket=ticket,
        ticket_repo=bundle.ticket_repo,
        catalog_repo=bundle.catalog_repo,
        state_manager=deps.state_manager,
        intake=cfg,
        ai_name=ai_name,
        think_tags=tuple(llm_cfg.think_tags),
    )
    agent = build_intake_agent(create_llm(llm_cfg, cfg.model), cfg)
    messages = await build_initial_messages(context, prompts)

    usage = _Usage()
    started = perf_counter()
    try:
        terminal_done = False
        async for update in agent.astream({"messages": messages}, context=context, stream_mode="updates"):
            terminal_done |= await _journal_update(deps, processing_id, context, update, usage)

        if not terminal_done:
            await _epilogue(context, deps, processing_id)
    finally:
        # The point of the whole A/B: what this run actually cost. Recorded
        # even when the run failed — a burnt budget is data too.
        await deps.journal.add_step(processing_id, "usage", usage.describe(perf_counter() - started))


@dataclass
class _Usage:
    """Token and call accounting for one agent run."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, message: AIMessage) -> None:
        self.model_calls += 1
        metadata = message.usage_metadata or {}
        self.input_tokens += metadata.get("input_tokens", 0)
        self.output_tokens += metadata.get("output_tokens", 0)

    def describe(self, elapsed: float) -> str:
        return (
            f"model calls: {self.model_calls}; "
            f"tokens in/out: {self.input_tokens}/{self.output_tokens}; "
            f"wall time: {elapsed:.1f}s"
        )


async def _journal_update(
    deps: AppDeps, processing_id: UUID, context: IntakeContext, update: dict, usage: _Usage
) -> bool:
    """Turn one stream update into journal steps; report a terminal tool result.

    Journalling happens here rather than inside the tools: the A/B comparison
    is about the model's *decisions* — which tool it picked, with which
    arguments, what it wrote as plain text — and that lives in the AIMessage,
    which a tool cannot see. Journal writes are non-fatal by contract; from
    inside a tool a failure would surface to the model as a tool error.
    """
    terminal = False
    for output in update.values():
        if not isinstance(output, dict):
            continue
        for message in output.get("messages") or []:
            if isinstance(message, AIMessage):
                usage.add(message)
                await deps.journal.add_step(processing_id, "agent", describe_ai_message(message, context.think_tags))
            elif isinstance(message, ToolMessage):
                detail = f"[{message.status}] {message.text[:300]}"
                await deps.journal.add_step(processing_id, f"tool:{message.name}", detail)
                terminal |= is_terminal_result(message)
    return terminal


async def _epilogue(context: IntakeContext, deps: AppDeps, processing_id: UUID) -> None:
    """Close a run the agent did not close itself.

    The model answered in plain text, burned `max_iterations` or looped.
    Setting `ai_done` is mandatory: otherwise the next webhook replays the
    whole expensive cycle to the same end.

    The `ai_done` re-read guards a real race: `handle_assigned` takes no lock,
    so an engineer can pick the ticket up while the agent is still thinking —
    and a fallback note posted after that would land on someone else's ticket.
    """
    ticket = context.ticket
    state = await deps.state_manager.get(ticket.label)
    if state.ai_done:
        await deps.journal.add_step(processing_id, "epilogue", "ticket already finished — nothing to close")
        return

    logger.info(f"[{processing_id}] {ticket.label}: agent produced no terminal action, posting fallback note")
    await context.ticket_repo.append_private_log(ticket, context.intake.handoff_fallback_note)
    await deps.state_manager.mark_done(ticket.label)
    await deps.journal.add_step(processing_id, "epilogue", "no terminal action — fallback note posted, ai_done set")
