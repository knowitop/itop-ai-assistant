import logging
from uuid import UUID

from graph.enrichment.context import GraphContext
from graph.enrichment.graph import graph
from itop.client import itop_client
from itop.utils import ticket_label
from state.ticket_state import state_manager

from .models import TicketEvent, WebhookPayload

logger = logging.getLogger(__name__)


async def _run_enrichment_graph(ticket, processing_id: UUID):
    logger.info(f"{processing_id} Running enrichment graph for {ticket_label(ticket)}")

    await graph.ainvoke(
        {
            "ticket": ticket,
            "action": None,
            "question": None,
        },
        context=GraphContext(itop_client=itop_client, state_manager=state_manager, processing_id=processing_id),
    )


async def process_webhook_logic(payload: WebhookPayload, processing_id: UUID):
    match payload.event:
        case TicketEvent.CREATED | TicketEvent.USER_COMMENTED:
            ticket = await itop_client.schema(payload.obj_class).find_one({"id": payload.id})
            await _run_enrichment_graph(ticket, processing_id)

        case TicketEvent.ASSIGNED:
            label = f"{payload.obj_class}::{payload.id}"
            await state_manager.mark_done(label)
            logger.info(f"[{processing_id}] {label} assigned, marked done")
