"""Enrichment module: webhook events → enrichment graph.

Registers itself in the pipeline registry for the classes listed in
`enrichment.classes` and owns the full event handling: per-ticket lock,
fetch, graph run, mark-done on assignment.
"""

import logging
from uuid import UUID

from config import EnrichmentConfig, Settings
from deps import AppDeps, create_llm
from domain.ticket import Ticket
from pipelines.registry import ModuleInfo, PipelineRegistry
from webhook.models import TicketEvent, WebhookPayload

from .context import GraphContext
from .graph import graph
from .prompts import PROMPT_VARIABLES, build_enrichment_prompts

logger = logging.getLogger(__name__)


def register(registry: PipelineRegistry, settings: Settings) -> None:
    cfg = settings.enrichment
    if not cfg.enabled:
        logger.info("Enrichment module is disabled, skipping registration")
        return

    info = ModuleInfo(
        name="enrichment",
        description="First-contact ticket enrichment: classify, evaluate completeness, ask, enrich",
        config_model=EnrichmentConfig,
        prompt_names=tuple(PROMPT_VARIABLES),
    )
    routes = {}
    for obj_class in cfg.classes:
        routes[(obj_class, str(TicketEvent.CREATED))] = handle_ticket_event
        routes[(obj_class, str(TicketEvent.USER_COMMENTED))] = handle_ticket_event
        routes[(obj_class, str(TicketEvent.ASSIGNED))] = handle_assigned
    registry.register(info, routes)


async def handle_ticket_event(payload: WebhookPayload, processing_id: UUID, deps: AppDeps) -> None:
    """Ticket created or user commented: run the enrichment graph under a per-ticket lock."""
    label = f"{payload.obj_class}::{payload.id}"

    if not await deps.state_manager.acquire_lock(label):
        logger.info(f"[{processing_id}] {label} is already being processed, skipping")
        return
    try:
        ticket = await deps.ticket_repo.fetch(payload.obj_class, payload.id)
        if ticket is None:
            logger.warning(f"[{processing_id}] {label} not found in iTop, skipping")
            return
        await _run_enrichment_graph(ticket, processing_id, deps)
    finally:
        await deps.state_manager.release_lock(label)


async def handle_assigned(payload: WebhookPayload, processing_id: UUID, deps: AppDeps) -> None:
    """Engineer took the ticket: stop any further AI processing."""
    label = f"{payload.obj_class}::{payload.id}"
    await deps.state_manager.mark_done(label)
    logger.info(f"[{processing_id}] {label} assigned, marked done")


async def _run_enrichment_graph(ticket: Ticket, processing_id: UUID, deps: AppDeps) -> None:
    logger.info(f"{processing_id} Running enrichment graph for {ticket.label}")

    enrichment = await deps.config_store.get_enrichment()
    prompts = build_enrichment_prompts(await deps.prompt_store.get("enrichment"))
    context = GraphContext(
        processing_id=processing_id,
        itop_client=deps.itop_client,
        ticket_repo=deps.ticket_repo,
        ticket_mapping=deps.settings.ticket_mapping,
        state_manager=deps.state_manager,
        enrichment=enrichment,
        prompts=prompts,
        llm_classify=create_llm(deps.settings, enrichment.classify_model),
        llm_evaluate=create_llm(deps.settings, enrichment.evaluate_model),
        llm_enrich=create_llm(deps.settings, enrichment.enrich_model),
        think_tags=tuple(deps.settings.llm_think_tags),
    )

    await graph.ainvoke(
        {
            "ticket": ticket,
            "action": None,
            "question": None,
        },
        context=context,
    )
