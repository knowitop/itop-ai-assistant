import logging
import re

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from config import get_settings
from itop.utils import ticket_label

from ..context import GraphContext
from ..state import Action, EnrichmentState
from .utils import bind_oql, build_conversation, html_to_markdown, strip_thinking

logger = logging.getLogger(__name__)

_s = get_settings()
_llm = ChatOpenAI(
    model_name=_s.llm_model,
    api_key=_s.llm_api_key,
    base_url=_s.llm_base_url,
)


def _build_prompt(system: str, human: str) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("human", human),
            MessagesPlaceholder("conversation"),
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


async def _invoke_and_extract(chain, invoke_vars: dict, id_tag: str) -> tuple[str | None, str]:
    response = await chain.ainvoke(invoke_vars)
    answer = strip_thinking(response.content)
    extracted_id = _extract_xml_field(answer, id_tag)
    confidence = _extract_xml_field(answer, "confidence") or "low"
    return extracted_id, confidence.lower()


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    cfg = get_settings().enrichment

    if not cfg.classification_enabled:
        return {}

    service_id: str = ticket["service_id"]
    subcategory_id: str = ticket["servicesubcategory_id"]

    if int(service_id) and int(subcategory_id):
        return {}

    itop_client = runtime.context.itop_client
    state_manager = runtime.context.state_manager
    label = ticket_label(ticket)

    title = ticket["title"]
    description = html_to_markdown(ticket["description"])

    ai_person = await itop_client.schema("Person").find_one({"id": ("=", ":current_contact_id")})
    caller_name = ticket["caller_id_friendlyname"]
    conversation = build_conversation(
        ticket["public_log"].get("entries") or [],
        ai_person["friendlyname"],
        caller_name,
    )

    new_service_id: str | None = None
    new_subcategory_id: str | None = None

    # Stage 1: classify service
    if not int(service_id):
        services_filter = bind_oql(cfg.classify_service_oql, ticket)
        services_list = await itop_client.schema("Service").find(
            services_filter, projection=["id", "name", "description"]
        )
        services_text = _format_services(services_list)
        valid_service_ids = {str(s["id"]) for s in services_list}

        chain = _build_prompt(cfg.classify_service_system_prompt, cfg.classify_service_human_prompt) | _llm
        extracted_id, confidence = await _invoke_and_extract(
            chain,
            {
                "caller_name": caller_name,
                "title": title,
                "description": description,
                "services": services_text,
                "conversation": conversation,
            },
            "service_id",
        )

        if confidence == "high" and extracted_id and extracted_id in valid_service_ids:
            logger.info(f"{label}: classified service_id={extracted_id}")
            new_service_id = extracted_id
            service_id = extracted_id
        else:
            logger.info(f"{label}: service classification confidence={confidence}, asking user")
            return await _ask_or_fallback(ticket, state_manager, itop_client, conversation)

    # Stage 2: classify subcategory
    if not int(subcategory_id):
        subcategories_filter = bind_oql(cfg.classify_subcategory_oql, {**ticket, "service_id": service_id})
        subcategories_list = await itop_client.schema("ServiceSubcategory").find(
            subcategories_filter,
            projection=["id", "name", "description"],
        )
        subcategories_text = _format_subcategories(subcategories_list)
        valid_subcategory_ids = {str(s["id"]) for s in subcategories_list}

        chain = _build_prompt(cfg.classify_subcategory_system_prompt, cfg.classify_subcategory_human_prompt) | _llm
        extracted_id, confidence = await _invoke_and_extract(
            chain,
            {
                "caller_name": caller_name,
                "title": title,
                "description": description,
                "subcategories": subcategories_text,
                "conversation": conversation,
            },
            "subcategory_id",
        )

        if confidence == "high" and extracted_id and extracted_id in valid_subcategory_ids:
            logger.info(f"{label}: classified servicesubcategory_id={extracted_id}")
            new_subcategory_id = extracted_id
        else:
            logger.info(f"{label}: subcategory classification confidence={confidence}, asking user")
            return await _ask_or_fallback(ticket, state_manager, itop_client, conversation)

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


async def _ask_or_fallback(ticket: dict, state_manager, itop_client, conversation: list) -> dict:
    label = ticket_label(ticket)
    ticket_state = await state_manager.get(label)
    cfg = get_settings().enrichment

    if ticket_state.classify_rounds >= cfg.max_classify_rounds:
        logger.info(f"{label}: classify rounds exhausted, fallback")
        await itop_client.schema(ticket["finalclass"]).update(
            {"id": ticket["id"]},
            {"private_log": {"add_item": {"message": cfg.classify_fallback_note, "format": "text"}}},
        )
        await state_manager.mark_done(label)
        return {"action": Action.STOP}
    chain = _build_prompt(cfg.classify_ask_system_prompt, cfg.classify_ask_human_prompt) | _llm
    response = await chain.ainvoke(
        {
            "caller_name": ticket["caller_id_friendlyname"],
            "title": ticket["title"],
            "description": html_to_markdown(ticket["description"]),
            "conversation": conversation,
        }
    )
    question = strip_thinking(response.content)

    await state_manager.increment_classify_rounds(label)
    logger.info(f"{label}: posting classify clarification question (round {ticket_state.classify_rounds + 1})")
    return {"action": Action.ASK, "question": question}
