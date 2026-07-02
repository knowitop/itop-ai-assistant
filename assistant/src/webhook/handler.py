import logging
from uuid import UUID

from deps import AppDeps, create_llm
from graph.enrichment.context import GraphContext
from graph.enrichment.graph import graph
from graph.enrichment.prompts import build_enrichment_prompts
from itop.utils import ticket_label

from .models import TicketEvent, WebhookPayload

logger = logging.getLogger(__name__)


async def _run_enrichment_graph(ticket: dict, processing_id: UUID, deps: AppDeps):
    logger.info(f"{processing_id} Running enrichment graph for {ticket_label(ticket)}")

    enrichment = await deps.config_store.get_enrichment()
    prompts = build_enrichment_prompts(await deps.prompt_store.get("enrichment"))
    context = GraphContext(
        processing_id=processing_id,
        itop_client=deps.itop_client,
        state_manager=deps.state_manager,
        enrichment=enrichment,
        prompts=prompts,
        llm_classify=create_llm(deps.settings, enrichment.classify_model),
        llm_evaluate=create_llm(deps.settings, enrichment.evaluate_model),
        llm_enrich=create_llm(deps.settings, enrichment.enrich_model),
    )

    await graph.ainvoke(
        {
            "ticket": ticket,
            "action": None,
            "question": None,
        },
        context=context,
    )


async def process_webhook_logic(payload: WebhookPayload, processing_id: UUID, deps: AppDeps):
    label = f"{payload.obj_class}::{payload.id}"

    match payload.event:
        case TicketEvent.CREATED | TicketEvent.USER_COMMENTED:
            if not await deps.state_manager.acquire_lock(label):
                logger.info(f"[{processing_id}] {label} is already being processed, skipping")
                return
            try:
                ticket = await deps.itop_client.schema(payload.obj_class).find_one({"id": payload.id})
                if ticket is None:
                    logger.warning(f"[{processing_id}] {label} not found in iTop, skipping")
                    return
                await _run_enrichment_graph(ticket, processing_id, deps)
            finally:
                await deps.state_manager.release_lock(label)

        case TicketEvent.ASSIGNED:
            await deps.state_manager.mark_done(label)
            logger.info(f"[{processing_id}] {label} assigned, marked done")
