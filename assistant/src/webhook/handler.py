import logging
import os
from uuid import UUID

from graph.enrichment.context import GraphContext
from graph.enrichment.graph import graph
from state.ticket_state import state_manager

from .models import TicketEvent, WebhookPayload

logger = logging.getLogger(__name__)


def _create_itop_client():
    from itop_client import Itop

    return Itop(
        url=os.getenv("ITOP_URL", "http://localhost/webservices/rest.php"),
        version="1.3",
        auth_user=os.getenv("ITOP_USER"),
        auth_pwd=os.getenv("ITOP_PWD"),
        auth_token=os.getenv("ITOP_TOKEN"),
    )


itop = _create_itop_client()


async def _run_enrichment_graph(ticket, processing_id: UUID):
    logger.info(f"{processing_id} Running enrichment graph for #{ticket['id']}")

    await graph.ainvoke(
        {
            "ticket": ticket,
            "action": None,
            "question": None,
        },
        context=GraphContext(itop_client=itop, state_manager=state_manager, processing_id=processing_id),
    )


async def process_webhook_logic(payload: WebhookPayload, processing_id: UUID):
    match payload.event:
        case TicketEvent.CREATED | TicketEvent.USER_COMMENTED:
            ticket = await itop.schema(payload.obj_class).find({"id": payload.id})
            await _run_enrichment_graph(ticket, processing_id)

        case TicketEvent.ASSIGNED:
            await state_manager.mark_done(payload.id)
            logger.info(f"[{processing_id}] Ticket #{payload.id} assigned, marked done")
