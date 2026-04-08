import logging

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from config import get_settings
from itop.utils import ticket_label

from ..context import GraphContext
from ..state import EnrichmentState
from .utils import strip_thinking

logger = logging.getLogger(__name__)

_s = get_settings()
_llm = ChatOpenAI(
    api_key=_s.llm_api_key,
    base_url=_s.llm_base_url,
    model_name=_s.llm_model,
)


def _build_enrich_prompt() -> ChatPromptTemplate:
    cfg = get_settings().enrichment
    return ChatPromptTemplate.from_messages(
        [
            ("system", cfg.enrich_system_prompt),
            ("human", cfg.enrich_human_prompt),
        ]
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]

    note = await _generate_note(ticket)

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


async def _generate_note(ticket: dict) -> str:
    log_text = (
        "\n".join(f"[{e['user_login']} at {e['date']}]: {e['message']}\n" for e in ticket["public_log"]["entries"])
        or "No comments yet"
    )

    prompt = _build_enrich_prompt()
    chain = prompt | _llm

    response = await chain.ainvoke(
        {
            "caller_name": ticket["caller_id_friendlyname"],
            "title": ticket["title"],
            "description": ticket["description"],
            "log_text": log_text,
        }
    )
    return strip_thinking(response.content)
