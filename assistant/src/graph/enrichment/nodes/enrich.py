import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import SecretStr

from ..context import GraphContext
from ..state import EnrichmentState
from .utils import strip_thinking

load_dotenv()

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_llm = ChatOpenAI(
    api_key=SecretStr(os.getenv("LLM_API_KEY")),
    base_url=os.getenv("LLM_BASE_URL"),
    model_name=os.getenv("LLM_MODEL"),
)


def _load_prompt(name: str) -> ChatPromptTemplate:
    with open(_PROMPTS_DIR / name) as f:
        data = yaml.safe_load(f)
    return ChatPromptTemplate.from_messages(
        [
            ("system", data["system"]),
            ("human", data["human"]),
        ]
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]

    note = await _generate_note(ticket)

    await runtime.context.itop_client.schema(ticket["finalclass"]).update(
        {"id": ticket["id"]},
        {"private_log": {"add_item": {"message": note, "format": "text"}}},
    )
    await runtime.context.state_manager.mark_done(ticket["ref"])

    logger.info(f"Ticket #{ticket['id']}: enriched and marked done")

    return {}


async def _generate_note(ticket: dict) -> str:
    log_text = (
        "\n".join(f"[{e['user_login']} at {e['date']}]: {e['message']}\n" for e in ticket["public_log"]["entries"])
        or "No comments yet"
    )

    prompt = _load_prompt("enrich.yaml")
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
