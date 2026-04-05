import logging
import os
from typing import Optional

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, SecretStr

from ..context import GraphContext
from ..state import Action, EnrichmentState

load_dotenv()

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2

_llm = ChatOpenAI(
    model_name=os.getenv("LLM_MODEL"),
    api_key=SecretStr(os.getenv("LLM_API_KEY")),
    base_url=os.getenv("LLM_BASE_URL"),
)


def _system_prompt(service: dict, subcategory: dict) -> str:
    return f"""
You are an IT support assistant. Your task is to evaluate whether a support
ticket contains enough information for an engineer to start working on it.

Service: {service["name"]}
Service description: {service["description"]}
Service subcategory: {subcategory["name"]}
Subcategory description: {subcategory["description"]}

Rules:
- If the ticket is sufficient, set sufficient=true.
- If critical information is missing, set sufficient=false and ask exactly one question — the most important missing piece.
- Write the question in the same language as the ticket.
- Be concise and friendly.
- Do not ask for information that is already in the description.
""".strip()


def _user_prompt(ticket: dict) -> str:
    # TODO: идентификатор автора комментария и тикета должен совпадать, чтобы LLM поняла, где его ответ
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


class EvaluationResult(BaseModel):
    sufficient: bool = Field(description="True if the ticket has enough information for the engineer to start working.")
    question: Optional[str] = Field(
        default=None, description="Single clarifying question to ask the user. Required if sufficient=False."
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]
    ticket_state = await runtime.context.state_manager.get(ticket["ref"])

    if ticket_state.rounds >= MAX_ROUNDS:
        logger.info(f"Ticket #{ticket['id']}: rounds exhausted, moving to enrich")
        return {"action": Action.ENRICH}
    itop_client = runtime.context.itop_client
    service = await itop_client.schema("Service").find({"id": ticket["service_id"]})
    service_subcategory = await itop_client.schema("ServiceSubcategory").find({"id": ticket["servicesubcategory_id"]})
    result = await _evaluate(ticket, service, service_subcategory)

    if result.sufficient:
        logger.info(f"Ticket #{ticket['id']}: description sufficient, moving to enrich")
        return {"action": Action.ENRICH}

    logger.info(f"Ticket #{ticket['id']}: incomplete, will ask question")
    return {"action": Action.ASK, "question": result.question}


async def _evaluate(ticket: dict, service: dict, subcategory: dict) -> EvaluationResult:
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _system_prompt(service, subcategory)),
            ("human", "{user_prompt}"),
        ]
    )
    chain = prompt | _llm.with_structured_output(EvaluationResult)

    result: EvaluationResult = await chain.ainvoke({"user_prompt": _user_prompt(ticket)})

    return result
