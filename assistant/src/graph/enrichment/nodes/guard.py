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

    # Loop protection, second line of defense after iTop trigger contexts:
    # if our own question is the last public entry, wait for the user instead
    # of reacting to our own comment or a duplicate webhook.
    if ticket.public_log:
        ai_name = await runtime.context.ticket_repo.get_ai_person_name()
        if ticket.public_log[-1].user_login == ai_name:
            logger.info(f"{ticket.label}: last public entry is ours, waiting for user reply, stopping")
            return {"action": Action.STOP}

    return {}
