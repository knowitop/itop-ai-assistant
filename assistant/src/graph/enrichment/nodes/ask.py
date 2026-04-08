import logging

from langgraph.runtime import Runtime

from itop.utils import ticket_label

from ..context import GraphContext
from ..state import EnrichmentState

logger = logging.getLogger(__name__)


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    question = state["question"]

    await runtime.context.itop_client.schema(ticket["finalclass"]).update(
        {"id": ticket["id"]}, {"public_log": {"add_item": {"message": question, "format": "text"}}}
    )
    await runtime.context.state_manager.increment_rounds(ticket_label(ticket))

    return {}
