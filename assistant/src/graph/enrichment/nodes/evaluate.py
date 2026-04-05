import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, SecretStr, model_validator

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
    # TODO: идентификатор автора комментария и тикета должен совпадать, чтобы LLM поняла, где его ответ
    log_text = (
        "\n".join(f"[{e['user_login']} at {e['date']}]: {e['message']}" for e in ticket["public_log"]["entries"])
        or "No comments yet"
    )

    prompt = _load_prompt("evaluate.yaml")
    chain = prompt | _llm.with_structured_output(EvaluationResult)

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
