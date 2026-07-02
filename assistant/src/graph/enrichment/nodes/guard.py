import logging

from langgraph.runtime import Runtime

from ..context import GraphContext
from ..state import Action, EnrichmentState

logger = logging.getLogger(__name__)


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    ticket_state = await runtime.context.state_manager.get(ticket.label)
    if ticket_state.ai_done:
        logger.info(f"{ticket.label}: already processed, stopping")
        return {"action": Action.STOP}

    active_statuses = runtime.context.ticket_mapping.active_statuses
    if ticket.status not in active_statuses:
        logger.info(f"{ticket.label}: status={ticket.status} not in {active_statuses}, stopping")
        return {"action": Action.STOP}

    return {}
