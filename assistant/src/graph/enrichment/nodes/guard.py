import logging

from langgraph.runtime import Runtime

from itop.models import TicketStatus

from ..context import GraphContext
from ..state import Action, EnrichmentState

logger = logging.getLogger(__name__)


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    ticket_state = await runtime.context.state_manager.get(ticket["ref"])
    if ticket_state.ai_done:
        logger.info(f"Ticket #{ticket['id']}: already processed, stopping")
        return {"action": Action.STOP}

    if ticket["status"] != TicketStatus.NEW:
        logger.info(f"Ticket #{ticket['id']}: status={ticket['status']}, stopping")
        return {"action": Action.STOP}

    return {}
