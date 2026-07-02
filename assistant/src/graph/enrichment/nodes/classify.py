import logging

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.runtime import Runtime

from domain.catalog import Service, ServiceSubcategory
from domain.ticket import Ticket

from ..context import GraphContext
from ..state import Action, EnrichmentState
from .utils import bind_oql, build_conversation, extract_xml_field, html_to_markdown, strip_thinking

logger = logging.getLogger(__name__)


def _build_prompt(system: str, human: str) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("human", human),
            MessagesPlaceholder("conversation"),
        ]
    )


def _format_options(options: list[Service] | list[ServiceSubcategory]) -> str:
    lines = []
    for opt in options:
        line = f"- ID {opt.id}: {opt.name}"
        desc = opt.description.strip()
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


async def _invoke_and_extract(
    chain, invoke_vars: dict, id_tag: str, think_tags: tuple[str, ...]
) -> tuple[str | None, str]:
    response = await chain.ainvoke(invoke_vars)
    answer = strip_thinking(response.content, think_tags)
    extracted_id = extract_xml_field(answer, id_tag)
    confidence = extract_xml_field(answer, "confidence") or "low"
    return extracted_id, confidence.lower()


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    cfg = runtime.context.enrichment

    if not cfg.classification_enabled:
        return {}

    if ticket.has_service and ticket.has_subcategory:
        return {}

    catalog = runtime.context.catalog_repo
    llm = runtime.context.llm_classify
    prompts = runtime.context.prompts

    title = ticket.title
    description = html_to_markdown(ticket.description)

    ai_name = await runtime.context.ticket_repo.get_ai_person_name()
    conversation = build_conversation(ticket.public_log, ai_name, ticket.caller_name)

    service_id = ticket.service_id
    new_service_id: str | None = None
    new_subcategory_id: str | None = None

    # Stage 1: classify service
    if not ticket.has_service:
        services_filter = bind_oql(cfg.classify_service_oql, ticket.model_dump())
        services_list = await catalog.find_services(services_filter)
        services_text = _format_options(services_list)
        valid_service_ids = {item.id for item in services_list}

        chain = _build_prompt(prompts.classify_service_system, prompts.classify_service_human) | llm
        extracted_id, confidence = await _invoke_and_extract(
            chain,
            {
                "caller_name": ticket.caller_name,
                "title": title,
                "description": description,
                "services": services_text,
                "conversation": conversation,
            },
            "service_id",
            runtime.context.think_tags,
        )

        if confidence == "high" and extracted_id and extracted_id in valid_service_ids:
            logger.info(f"{ticket.label}: classified service_id={extracted_id}")
            new_service_id = extracted_id
            service_id = extracted_id
        else:
            logger.info(f"{ticket.label}: service classification confidence={confidence}, asking user")
            return await _ask_or_fallback(ticket, runtime, conversation)

    # Stage 2: classify subcategory
    if not ticket.has_subcategory:
        subcategories_filter = bind_oql(cfg.classify_subcategory_oql, {**ticket.model_dump(), "service_id": service_id})
        subcategories_list = await catalog.find_subcategories(subcategories_filter)
        subcategories_text = _format_options(subcategories_list)
        valid_subcategory_ids = {item.id for item in subcategories_list}

        chain = _build_prompt(prompts.classify_subcategory_system, prompts.classify_subcategory_human) | llm
        extracted_id, confidence = await _invoke_and_extract(
            chain,
            {
                "caller_name": ticket.caller_name,
                "title": title,
                "description": description,
                "subcategories": subcategories_text,
                "conversation": conversation,
            },
            "subcategory_id",
            runtime.context.think_tags,
        )

        if confidence == "high" and extracted_id and extracted_id in valid_subcategory_ids:
            logger.info(f"{ticket.label}: classified subcategory_id={extracted_id}")
            new_subcategory_id = extracted_id
        else:
            logger.info(f"{ticket.label}: subcategory classification confidence={confidence}, asking user")
            return await _ask_or_fallback(ticket, runtime, conversation)

    # Update iTop once with all newly determined fields
    if new_service_id or new_subcategory_id:
        update_fields: dict = {}
        if new_service_id:
            update_fields["service_id"] = new_service_id
        if new_subcategory_id:
            update_fields["subcategory_id"] = new_subcategory_id

        await runtime.context.ticket_repo.set_fields(ticket, update_fields)

        updated_ticket = ticket.model_copy(update=update_fields)
        return {"ticket": updated_ticket}

    return {}


async def _ask_or_fallback(ticket: Ticket, runtime: Runtime[GraphContext], conversation: list) -> dict:
    ctx = runtime.context
    cfg = ctx.enrichment
    ticket_state = await ctx.state_manager.get(ticket.label)

    if ticket_state.classify_rounds >= cfg.max_classify_rounds:
        logger.info(f"{ticket.label}: classify rounds exhausted, fallback")
        await ctx.ticket_repo.append_private_log(ticket, cfg.classify_fallback_note)
        await ctx.state_manager.mark_done(ticket.label)
        return {"action": Action.STOP}

    chain = _build_prompt(ctx.prompts.classify_ask_system, ctx.prompts.classify_ask_human) | ctx.llm_classify
    response = await chain.ainvoke(
        {
            "caller_name": ticket.caller_name,
            "title": ticket.title,
            "description": html_to_markdown(ticket.description),
            "conversation": conversation,
        }
    )
    question = strip_thinking(response.content, ctx.think_tags)

    await ctx.state_manager.increment_classify_rounds(ticket.label)
    logger.info(f"{ticket.label}: posting classify clarification question (round {ticket_state.classify_rounds + 1})")
    return {"action": Action.ASK, "question": question}
