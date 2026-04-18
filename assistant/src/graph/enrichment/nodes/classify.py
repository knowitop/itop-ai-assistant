import logging
import re

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from config import get_settings
from itop.utils import ticket_label

from ..context import GraphContext
from ..state import Action, EnrichmentState
from .utils import html_to_markdown, strip_thinking

logger = logging.getLogger(__name__)

MAX_CLASSIFY_ROUNDS = 2

_s = get_settings()
_llm = ChatOpenAI(
    model_name=_s.llm_model,
    api_key=_s.llm_api_key,
    base_url=_s.llm_base_url,
)

_FALLBACK_NOTE = "Не удалось определить категорию обращения. Требуется ручная классификация."


def _build_service_prompt() -> ChatPromptTemplate:
    cfg = get_settings().enrichment
    return ChatPromptTemplate.from_messages(
        [
            ("system", cfg.classify_service_system_prompt),
            ("human", cfg.classify_service_human_prompt),
        ]
    )


def _build_subcategory_prompt() -> ChatPromptTemplate:
    cfg = get_settings().enrichment
    return ChatPromptTemplate.from_messages(
        [
            ("system", cfg.classify_subcategory_system_prompt),
            ("human", cfg.classify_subcategory_human_prompt),
        ]
    )


def _build_ask_prompt() -> ChatPromptTemplate:
    cfg = get_settings().enrichment
    return ChatPromptTemplate.from_messages(
        [
            ("system", cfg.classify_ask_system_prompt),
            ("human", cfg.classify_ask_human_prompt),
        ]
    )


def _extract_xml_field(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip()
    return value if value else None


def _format_services(services: list[dict]) -> str:
    lines = []
    for svc in services:
        line = f"- ID {svc['id']}: {svc['name']}"
        desc = (svc["description"] or "").strip()
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


def _format_subcategories(subcategories: list[dict]) -> str:
    lines = []
    for sub in subcategories:
        line = f"- ID {sub['id']}: {sub['name']}"
        desc = (sub["description"] or "").strip()
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    cfg = get_settings().enrichment

    if not cfg.classification_enabled:
        return {}

    service_id: str = ticket["service_id"]
    subcategory_id: str = ticket["servicesubcategory_id"]

    if service_id and subcategory_id:
        return {}

    itop_client = runtime.context.itop_client
    state_manager = runtime.context.state_manager
    label = ticket_label(ticket)

    title = ticket["title"]
    description = html_to_markdown(ticket["description"])

    new_service_id: str | None = None
    new_subcategory_id: str | None = None

    # Stage 1: classify service
    if not service_id:
        raw_services = await itop_client.schema("Service").find(
            {"status": "production"}, projection=["id", "name", "description"]
        )
        services_list = raw_services if isinstance(raw_services, list) else [raw_services] if raw_services else []
        services_text = _format_services(services_list)
        valid_service_ids = {str(s["id"]) for s in services_list}

        prompt = _build_service_prompt()
        chain = prompt | _llm
        response = await chain.ainvoke({"title": title, "description": description, "services": services_text})
        answer = strip_thinking(response.content)

        extracted_id = _extract_xml_field(answer, "service_id")
        confidence = _extract_xml_field(answer, "confidence") or "low"

        if confidence.lower() == "high" and extracted_id and extracted_id in valid_service_ids:
            logger.info(f"{label}: classified service_id={extracted_id}")
            new_service_id = extracted_id
            service_id = extracted_id
        else:
            logger.info(f"{label}: service classification confidence={confidence}, asking user")
            return await _ask_or_fallback(ticket, state_manager, itop_client, title, description)

    # Stage 2: classify subcategory
    if not subcategory_id:
        raw_subcategories = await itop_client.schema("ServiceSubcategory").find(
            {"service_id": service_id, "status": "production"},
            projection=["id", "name", "description"],
        )
        subcategories_list = (
            raw_subcategories
            if isinstance(raw_subcategories, list)
            else [raw_subcategories]
            if raw_subcategories
            else []
        )
        subcategories_text = _format_subcategories(subcategories_list)
        valid_subcategory_ids = {str(s["id"]) for s in subcategories_list}

        prompt = _build_subcategory_prompt()
        chain = prompt | _llm
        response = await chain.ainvoke(
            {"title": title, "description": description, "subcategories": subcategories_text}
        )
        answer = strip_thinking(response.content)

        extracted_id = _extract_xml_field(answer, "subcategory_id")
        confidence = _extract_xml_field(answer, "confidence") or "low"

        if confidence.lower() == "high" and extracted_id and extracted_id in valid_subcategory_ids:
            logger.info(f"{label}: classified servicesubcategory_id={extracted_id}")
            new_subcategory_id = extracted_id
        else:
            logger.info(f"{label}: subcategory classification confidence={confidence}, asking user")
            return await _ask_or_fallback(ticket, state_manager, itop_client, title, description)

    # Update iTop once with all newly determined fields
    if new_service_id or new_subcategory_id:
        update_fields: dict = {}
        if new_service_id:
            update_fields["service_id"] = new_service_id
        if new_subcategory_id:
            update_fields["servicesubcategory_id"] = new_subcategory_id

        await itop_client.schema(ticket["finalclass"]).update({"id": ticket["id"]}, update_fields)

        updated_ticket = dict(ticket)
        if new_service_id:
            updated_ticket["service_id"] = new_service_id
        if new_subcategory_id:
            updated_ticket["servicesubcategory_id"] = new_subcategory_id
        return {"ticket": updated_ticket}

    return {}


async def _ask_or_fallback(
    ticket: dict,
    state_manager,
    itop_client,
    title: str,
    description: str,
) -> dict:
    label = ticket_label(ticket)
    ticket_state = await state_manager.get(label)

    if ticket_state.classify_rounds >= MAX_CLASSIFY_ROUNDS:
        logger.info(f"{label}: classify rounds exhausted, fallback")
        await itop_client.schema(ticket["finalclass"]).update(
            {"id": ticket["id"]},
            {"private_log": {"add_item": {"message": _FALLBACK_NOTE, "format": "text"}}},
        )
        await state_manager.mark_done(label)
        return {"action": Action.STOP}

    prompt = _build_ask_prompt()
    chain = prompt | _llm
    response = await chain.ainvoke({"title": title, "description": description})
    question = strip_thinking(response.content)

    await state_manager.increment_classify_rounds(label)
    logger.info(f"{label}: posting classify clarification question (round {ticket_state.classify_rounds + 1})")
    return {"action": Action.ASK, "question": question}
