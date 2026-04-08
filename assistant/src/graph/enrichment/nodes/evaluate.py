import logging

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from config import get_settings
from itop.utils import ticket_label
from itop_client import Itop

from ..context import GraphContext
from ..state import Action, EnrichmentState
from .utils import html_to_markdown, strip_thinking

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2

_s = get_settings()
_llm = ChatOpenAI(
    model_name=_s.llm_model,
    api_key=_s.llm_api_key,
    base_url=_s.llm_base_url,
)


def _build_evaluate_prompt() -> ChatPromptTemplate:
    cfg = get_settings().enrichment
    return ChatPromptTemplate.from_messages(
        [
            ("system", cfg.evaluate_system_prompt),
            ("human", cfg.evaluate_human_prompt),
            MessagesPlaceholder("conversation"),
        ]
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]

    if not _has_service_context(ticket):
        logger.info(f"{ticket_label(ticket)}: no service context, moving to enrich")
        return {"action": Action.ENRICH}

    ticket_state = await runtime.context.state_manager.get(ticket_label(ticket))
    if ticket_state.rounds >= MAX_ROUNDS:
        logger.info(f"{ticket_label(ticket)}: rounds exhausted, moving to enrich")
        return {"action": Action.ENRICH}

    service_context = await _build_service_context(ticket, runtime.context.itop_client)

    ai_person = await runtime.context.itop_client.schema("Person").find({"id": ("=", ":current_contact_id")})
    caller_name = ticket["caller_id_friendlyname"]
    conversation = _build_conversation(
        ticket["public_log"].get("entries") or [], ai_person["friendlyname"], caller_name
    )

    prompt = _build_evaluate_prompt()
    chain = prompt | _llm
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
    question = None if answer.upper() == "SUFFICIENT" else answer

    if question is None:
        logger.info(f"{ticket_label(ticket)}: description sufficient, moving to enrich")
        return {"action": Action.ENRICH}

    logger.info(f"{ticket_label(ticket)}: incomplete, will ask question")
    return {"action": Action.ASK, "question": question}


def _has_service_context(ticket: dict) -> bool:
    return bool(int(ticket["service_id"]))


async def _build_service_context(ticket: dict, itop_client: Itop) -> str:
    service = await itop_client.schema("Service").find({"id": ticket["service_id"]})
    service_subcategory = await itop_client.schema("ServiceSubcategory").find({"id": ticket["servicesubcategory_id"]})

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


def _build_conversation(entries: list, ai_name: str, caller_name: str) -> list:
    messages = []
    for e in entries:
        if e["user_login"] == ai_name:
            messages.append(AIMessage(content=e["message"]))
        else:
            user_prefix = e["user_login"]
            if e["user_login"] == caller_name:
                user_prefix += " [Requester]"
            messages.append(HumanMessage(content=f"{user_prefix}: {e['message']}", name=e["user_login"]))
    return messages
