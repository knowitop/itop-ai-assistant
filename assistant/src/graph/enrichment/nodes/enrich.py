import logging
import os

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, SecretStr

from ..context import GraphContext
from ..state import EnrichmentState

load_dotenv()

logger = logging.getLogger(__name__)


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
    chain = (
        ChatPromptTemplate.from_messages(
            [
                ("system", _system_prompt(service, subcategory)),
                ("human", "{user_prompt}"),
            ]
        )
        | _llm
    )

    return await chain.ainvoke({"user_prompt": _user_prompt(ticket)})


def _system_prompt(service: dict, subcategory: dict) -> str:
    return f"""
You are an IT support assistant preparing a handoff note for an engineer.
Summarize the ticket concisely based on the user's description and conversation.

Service: {service["name"]}
Service description: {service["description"]}
Service subcategory: {subcategory["name"]}
Subcategory description: {subcategory["description"]}

Return a structured note with four fields:
- problem: one sentence describing the issue
- details: key technical details (device, OS, version, when it started)
- already_tried: what the user has already attempted, or "Nothing mentioned"
- attachments: description of attachments if any, or "None"

Be concise. Write in the same language as the ticket.
""".strip()


def _user_prompt(ticket: dict) -> str:
    # TODO: идентификатор комментария и тикета должен совпадать, чтобы LLM поняла, где его ответ
    log_text = (
        "\n".join(f"[{e['user_login']} at {e['date']}]: {e['message']}" for e in ticket["public_log"]["entries"])
        or "No comments yet"
    )

    return f"""
User: {ticket["caller_id_friendlyname"]}

Title: {ticket["title"]}
Description: {ticket["description"]}

Conversation so far:
{log_text}
""".strip()


def _format_note(note: EngineerNote) -> str:
    return f"""[AI Summary]<br>
Problem:       {note.problem}<br>
Details:       {note.details}<br>
Already tried: {note.already_tried}<br>
Attachments:   {note.attachments}<br>"""
