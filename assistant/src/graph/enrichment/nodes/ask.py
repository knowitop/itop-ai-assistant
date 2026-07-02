import logging

from langgraph.runtime import Runtime

from ..context import GraphContext
from ..state import EnrichmentState

logger = logging.getLogger(__name__)


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    question = state["question"]

    await runtime.context.ticket_repo.append_public_log(ticket, question)
    await runtime.context.state_manager.increment_rounds(ticket.label)

    return {}
