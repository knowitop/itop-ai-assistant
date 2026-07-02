import logging

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.runtime import Runtime

from itop.utils import ticket_label
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

    if not _has_service_context(ticket):
        logger.info(f"{ticket_label(ticket)}: no service context, moving to enrich")
        return {"action": Action.ENRICH}

    ticket_state = await runtime.context.state_manager.get(ticket_label(ticket))
    if ticket_state.rounds >= cfg.max_rounds:
        logger.info(f"{ticket_label(ticket)}: rounds exhausted, moving to enrich")
        return {"action": Action.ENRICH}

    service_context = await _build_service_context(ticket, runtime.context.itop_client)

    # DRY: see nodes.enrich._generate_note
    ai_person = await runtime.context.itop_client.schema("Person").find_one({"id": ("=", ":current_contact_id")})
    caller_name = ticket["caller_id_friendlyname"]
    conversation = build_conversation(ticket["public_log"].get("entries") or [], ai_person["friendlyname"], caller_name)

    chain = _build_evaluate_prompt(runtime.context.prompts) | runtime.context.llm_evaluate
    response = await chain.ainvoke(
        {
            "service_context": service_context,
            "caller_name": caller_name,
            "title": ticket["title"],
            "description": html_to_markdown(ticket["description"]),
            "conversation": conversation,
        }
    )
    answer = strip_thinking(response.content)
    if not answer:
        logger.warning(f"{ticket_label(ticket)}: LLM returned empty response in evaluate, moving to enrich")
        return {"action": Action.ENRICH}
    question = None if "<result>SUFFICIENT</result>".upper() in answer.upper() else answer

    if question is None:
        logger.info(f"{ticket_label(ticket)}: description sufficient, moving to enrich")
        return {"action": Action.ENRICH}

    logger.info(f"{ticket_label(ticket)}: incomplete, will ask question")
    return {"action": Action.ASK, "question": question}


def _has_service_context(ticket: dict) -> bool:
    return bool(int(ticket["service_id"]))


async def _build_service_context(ticket: dict, itop_client: Itop) -> str:
    service = await itop_client.schema("Service").find_one({"id": ticket["service_id"]})
    service_subcategory = await itop_client.schema("ServiceSubcategory").find_one(
        {"id": ticket["servicesubcategory_id"]}
    )

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
