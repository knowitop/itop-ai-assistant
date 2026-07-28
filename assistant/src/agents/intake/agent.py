"""The intake agent: `create_agent` plus two middleware.

`system_prompt` is not passed to `create_agent` — the initial messages are
assembled by `prompt.py` and already start with a `SystemMessage`.

The agent is rebuilt for every run: the model comes from `intake.model` and
the budget from `intake.max_iterations`, both editable at runtime.
"""

import logging
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, before_model, wrap_tool_call
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from config import IntakeConfig

# Imported as-is from the enrichment module: a single generic helper is not
# worth a shared package while the two modules are competing.
from graph.enrichment.nodes.utils import strip_thinking

from .context import IntakeContext
from .tools import TOOLS, ToolRejection

logger = logging.getLogger(__name__)

# A successful call to one of these ends the session: one question or one
# handoff per run, never both.
TERMINAL_TOOLS = frozenset({"post_public_question", "finish_handoff"})


@wrap_tool_call
async def _tool_gate(request, handler):
    """Turn a tool's refusal into feedback for the model.

    Only `ToolRejection` is caught. Real failures (iTop down, Redis down)
    propagate and fail the run — same contract as the enrichment graph.
    Invalid arguments never reach here: `ToolNode` converts them into an
    error `ToolMessage` on its own.
    """
    try:
        return await handler(request)
    except ToolRejection as e:
        logger.info(f"intake: tool {request.tool_call['name']} rejected the call: {e}")
        return ToolMessage(
            content=str(e),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )


@before_model(can_jump_to=["end"])
def _stop_after_terminal(state, runtime) -> dict[str, Any] | None:
    """End the run once a terminal tool has succeeded.

    Runs before the model call, so the closing "look at the result and say
    something" round trip is never paid for. Note that returning
    `Command(goto="__end__")` from `wrap_tool_call` does *not* work here: the
    conditional edge `create_agent` puts on the tools node fires anyway.
    """
    for message in reversed(state["messages"]):
        if not isinstance(message, ToolMessage):
            break
        if message.name in TERMINAL_TOOLS and message.status == "success":
            return {"jump_to": "end"}
    return None


def build_intake_agent(llm: BaseChatModel, cfg: IntakeConfig) -> CompiledStateGraph:
    return create_agent(
        model=llm,
        tools=TOOLS,
        context_schema=IntakeContext,
        middleware=[
            _tool_gate,
            _stop_after_terminal,
            ModelCallLimitMiddleware(run_limit=cfg.max_iterations, exit_behavior="end"),
        ],
    )


def is_terminal_result(message: ToolMessage) -> bool:
    return message.name in TERMINAL_TOOLS and message.status == "success"


def describe_ai_message(message: AIMessage, think_tags: tuple[str, ...]) -> str:
    """One journal line for a model turn: which tools, with which arguments."""
    if message.tool_calls:
        calls = "; ".join(f"{call['name']}({call['args']})" for call in message.tool_calls)
        return f"calls: {calls}"
    # strip_thinking touches displayed text only — never tool-call arguments
    return f"no tool call: {strip_thinking(message.content, think_tags)[:300]}"
