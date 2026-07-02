import logging

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.runtime import Runtime

from itop.utils import ticket_label

from ..context import GraphContext
from ..prompts import EnrichmentPrompts
from ..state import EnrichmentState
from .utils import build_conversation, html_to_markdown, strip_thinking

logger = logging.getLogger(__name__)


def _build_enrich_prompt(prompts: EnrichmentPrompts) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", prompts.enrich_system),
            ("human", prompts.enrich_human),
            MessagesPlaceholder("conversation"),
        ]
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]

    note = await _generate_note(ticket, runtime)

    if note:
        await runtime.context.itop_client.schema(ticket["finalclass"]).update(
            {"id": ticket["id"]},
            {"private_log": {"add_item": {"message": note, "format": "text"}}},
        )
    else:
        logger.warning(f"{ticket_label(ticket)}: LLM returned empty note, skipping private log entry")
    await runtime.context.state_manager.mark_done(ticket_label(ticket))

    logger.info(f"{ticket_label(ticket)}: enriched and marked done")

    return {}


async def _generate_note(ticket: dict, runtime: Runtime[GraphContext]) -> str:
    ctx = runtime.context
    ai_person = await ctx.itop_client.schema("Person").find_one({"id": ("=", ":current_contact_id")})
    caller_name = ticket["caller_id_friendlyname"]
    conversation = build_conversation(ticket["public_log"].get("entries") or [], ai_person["friendlyname"], caller_name)

    chain = _build_enrich_prompt(ctx.prompts) | ctx.llm_enrich

    response = await chain.ainvoke(
        {
            "caller_name": caller_name,
            "title": ticket["title"],
            "description": html_to_markdown(ticket["description"]),
            "conversation": conversation,
        }
    )
    return strip_thinking(response.content)
