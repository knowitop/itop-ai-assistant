import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, SecretStr, model_validator

from itop_client import Itop

from ..context import GraphContext
from ..state import Action, EnrichmentState

load_dotenv()

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_llm = ChatOpenAI(
    model_name=os.getenv("LLM_MODEL"),
    api_key=SecretStr(os.getenv("LLM_API_KEY")),
    base_url=os.getenv("LLM_BASE_URL"),
)


class EvaluationResult(BaseModel):
    sufficient: bool = Field(description="True if the ticket has enough information for the engineer to start working.")
    question: Optional[str] = Field(
        default=None, description="Single message covering all missing items. Required if sufficient=False."
    )

    @model_validator(mode="after")
    def question_required_if_not_sufficient(self):
        if not self.sufficient and not self.question:
            raise ValueError("question must be provided when sufficient=False")
        return self


def _load_evaluate_prompt() -> ChatPromptTemplate:
    with open(_PROMPTS_DIR / "evaluate.yaml") as f:
        data = yaml.safe_load(f)
    return ChatPromptTemplate.from_messages(
        [
            ("system", data["system"]),
            ("human", data["human"]),
            MessagesPlaceholder("conversation"),
        ]
    )


async def run(state: EnrichmentState, runtime: Runtime[GraphContext]) -> dict:
    ticket = state["ticket"]

    if not _has_service_context(ticket):
        logger.info(f"Ticket #{ticket['id']}: no service context, moving to enrich")
        return {"action": Action.ENRICH}

    ticket_state = await runtime.context.state_manager.get(ticket["ref"])
    if ticket_state.rounds >= MAX_ROUNDS:
        logger.info(f"Ticket #{ticket['id']}: rounds exhausted, moving to enrich")
        return {"action": Action.ENRICH}

    service_context = await _build_service_context(ticket, runtime.context.itop_client)

    ai_person = await runtime.context.itop_client.schema("Person").find({"id": ("=", ":current_contact_id")})
    caller_name = ticket["caller_id_friendlyname"]
    conversation = _build_conversation(
        ticket["public_log"].get("entries") or [], ai_person["friendlyname"], caller_name
    )

    prompt = _load_evaluate_prompt()
    chain = prompt | _llm.with_structured_output(EvaluationResult)
    result: EvaluationResult = await chain.ainvoke(
        {
            "service_context": service_context,
            "caller_name": caller_name,
            "title": ticket["title"],
            "description": ticket["description"],
            "conversation": conversation,
        }
    )

    if result.sufficient:
        logger.info(f"Ticket #{ticket['id']}: description sufficient, moving to enrich")
        return {"action": Action.ENRICH}

    logger.info(f"Ticket #{ticket['id']}: incomplete, will ask question")
    return {"action": Action.ASK, "question": result.question}


def _has_service_context(ticket: dict) -> bool:
    return bool(int(ticket["service_id"]))


async def _build_service_context(ticket: dict, itop_client: Itop) -> str:
    service = await itop_client.schema("Service").find({"id": ticket["service_id"]})
    service_subcategory = await itop_client.schema("ServiceSubcategory").find({"id": ticket["servicesubcategory_id"]})

    parts = []

    if service:
        parts.append(f"Service: {service['name']}")
        if service["description"]:
            parts.append(f"Service description:\n{service['description']}")
    if service_subcategory:
        parts.append(f"Subcategory: {service_subcategory['name']}")
        if service_subcategory["description"]:
            parts.append(f"Subcategory description:\n{service_subcategory['description']}")

    if not parts:
        return "No service context provided."

    return "\n".join(parts)


def _build_conversation(entries: list, ai_name: str, caller_name: str) -> list:
    messages = []
    for e in entries:
        if e["user_login"] == ai_name:
            messages.append(AIMessage(content=e["message"]))
        else:
            user_prefix = e["user_login"]
            if e["user_login"] == caller_name:
                user_prefix += " [Requester]"
            messages.append(HumanMessage(content=f"{user_prefix}: {e['message']}", name=e["user_login"]))
    return messages
