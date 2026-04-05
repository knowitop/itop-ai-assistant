import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, SecretStr

from ..context import GraphContext
from ..state import EnrichmentState

load_dotenv()

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class EngineerNote(BaseModel):
    problem: str = Field(description="The core issue in one sentence.")
    details: str = Field(description="Key technical details: device, OS, version, when it started.")
    already_tried: str = Field(description="What the user has already attempted. Use 'Nothing mentioned' if absent.")
    attachments: str = Field(description="Description of any attachments. Use 'None' if absent.")


_llm = ChatOpenAI(
    api_key=SecretStr(os.getenv("LLM_API_KEY")),
    base_url=os.getenv("LLM_BASE_URL"),
    model_name=os.getenv("LLM_MODEL"),
).with_structured_output(EngineerNote)


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

    itop_client = runtime.context.itop_client
    # TODO: переделать в репозиторий с кешированием на короткое время
    service = await itop_client.schema("Service").find({"id": ticket["service_id"]})
    service_subcategory = await itop_client.schema("ServiceSubcategory").find({"id": ticket["servicesubcategory_id"]})

    note = await _generate_note(ticket, service, service_subcategory)
    formatted = _format_note(note)

    await runtime.context.itop_client.schema(ticket["finalclass"]).update(
        {"id": ticket["id"]},
        {"private_log": formatted},
    )
    await runtime.context.state_manager.mark_done(ticket["ref"])

    logger.info(f"Ticket #{ticket['id']}: enriched and marked done")

    return {}


async def _generate_note(ticket: dict, service: dict, subcategory: dict) -> EngineerNote:
    log_text = (
        "\n".join(f"[{e['user_login']} at {e['date']}]: {e['message']}" for e in ticket["public_log"]["entries"])
        or "No comments yet"
    )

    prompt = _load_prompt("enrich.yaml")
    chain = prompt | _llm

    return await chain.ainvoke(
        {
            "service_name": service["name"],
            "service_description": service["description"],
            "subcategory_name": subcategory["name"],
            "subcategory_description": subcategory["description"],
            "caller_name": ticket["caller_id_friendlyname"],
            "title": ticket["title"],
            "description": ticket["description"],
            "log_text": log_text,
        }
    )


def _format_note(note: EngineerNote) -> str:
    return f"""[AI Summary]<br>
Problem:       {note.problem}<br>
Details:       {note.details}<br>
Already tried: {note.already_tried}<br>
Attachments:   {note.attachments}<br>"""
