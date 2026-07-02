import logging

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.runtime import Runtime

from domain.ticket import Ticket
from itop_client import Itop

from ..context import GraphContext
from ..prompts import EnrichmentPrompts
from ..state import Action, EnrichmentState
from .utils import build_conversation, html_to_markdown, strip_thinking

logger = logging.getLogger(__name__)


def _build_evaluate_prompt(prompts: EnrichmentPrompts) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", prompts.evaluate_system),
            ("human", prompts.evaluate_human),
            MessagesPlaceholder("conversation"),
        ]
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    cfg = runtime.context.enrichment

    if not ticket.has_service:
        logger.info(f"{ticket.label}: no service context, moving to enrich")
        return {"action": Action.ENRICH}

    ticket_state = await runtime.context.state_manager.get(ticket.label)
    if ticket_state.rounds >= cfg.max_rounds:
        logger.info(f"{ticket.label}: rounds exhausted, moving to enrich")
        return {"action": Action.ENRICH}

    service_context = await _build_service_context(ticket, runtime.context.itop_client)

    ai_name = await runtime.context.ticket_repo.get_ai_person_name()
    conversation = build_conversation(ticket.public_log, ai_name, ticket.caller_name)

    chain = _build_evaluate_prompt(runtime.context.prompts) | runtime.context.llm_evaluate
    response = await chain.ainvoke(
        {
            "service_context": service_context,
            "caller_name": ticket.caller_name,
            "title": ticket.title,
            "description": html_to_markdown(ticket.description),
            "conversation": conversation,
        }
    )
    answer = strip_thinking(response.content)
    if not answer:
        logger.warning(f"{ticket.label}: LLM returned empty response in evaluate, moving to enrich")
        return {"action": Action.ENRICH}
    question = None if "<result>SUFFICIENT</result>".upper() in answer.upper() else answer

    if question is None:
        logger.info(f"{ticket.label}: description sufficient, moving to enrich")
        return {"action": Action.ENRICH}

    logger.info(f"{ticket.label}: incomplete, will ask question")
    return {"action": Action.ASK, "question": question}


async def _build_service_context(ticket: Ticket, itop_client: Itop) -> str:
    service = await itop_client.schema("Service").find_one({"id": ticket.service_id})
    service_subcategory = await itop_client.schema("ServiceSubcategory").find_one({"id": ticket.subcategory_id})

    parts = []

    if service:
        parts.append(f"Service: {service['name']}")
        if service["description"]:
            parts.append(f"Service description:\n{service['description']}")
    if service_subcategory:
        parts.append(f"Subcategory: {service_subcategory['name']}")
        if service_subcategory["description"]:
            parts.append(f"Subcategory description:\n{service_subcategory['description']}")

    if not parts:
        return "No service context provided."

    return "\n".join(parts)
