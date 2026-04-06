import logging
from uuid import UUID

from graph.enrichment.context import GraphContext
from graph.enrichment.graph import graph
from itop.client import itop_client
from state.ticket_state import state_manager

from .models import TicketEvent, WebhookPayload

logger = logging.getLogger(__name__)


async def _run_enrichment_graph(ticket, processing_id: UUID):
    logger.info(f"{processing_id} Running enrichment graph for #{ticket['id']}")

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
            ticket = await itop_client.schema(payload.obj_class).find({"id": payload.id})
            await _run_enrichment_graph(ticket, processing_id)

        case TicketEvent.ASSIGNED:
            await state_manager.mark_done(payload.id)
            logger.info(f"[{processing_id}] Ticket #{payload.id} assigned, marked done")
