import logging

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.runtime import Runtime

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

    note = await _generate_note(state, runtime)

    if note:
        await runtime.context.ticket_repo.append_private_log(ticket, note)
    else:
        logger.warning(f"{ticket.label}: LLM returned empty note, skipping private log entry")
    await runtime.context.state_manager.mark_done(ticket.label)

    logger.info(f"{ticket.label}: enriched and marked done")

    return {}


async def _generate_note(state: EnrichmentState, runtime: Runtime[GraphContext]) -> str:
    ticket = state["ticket"]
    ctx = runtime.context

    ai_name = await ctx.ticket_repo.get_ai_person_name()
    conversation = build_conversation(ticket.public_log, ai_name, ticket.caller_name)

    chain = _build_enrich_prompt(ctx.prompts) | ctx.llm_enrich

    response = await chain.ainvoke(
        {
            "caller_name": ticket.caller_name,
            "title": ticket.title,
            "description": html_to_markdown(ticket.description),
            "conversation": conversation,
        }
    )
    return strip_thinking(response.content)
