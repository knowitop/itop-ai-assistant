"""The intake agent: `create_agent` plus four middleware.

`system_prompt` is not passed to `create_agent` — the initial messages are
assembled by `prompt.py` and already start with a `SystemMessage`.

The agent is rebuilt for every run: the model comes from `intake.model` and
the budget from `intake.max_iterations`, both editable at runtime.
"""

import logging
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelResponse,
    before_model,
    wrap_model_call,
    wrap_tool_call,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from config import IntakeConfig
from text_utils import strip_thinking

from .context import IntakeContext
from .tools import ToolRejection

logger = logging.getLogger(__name__)

# A successful call to one of these ends the session: one question or one
# handoff per run, never both.
TERMINAL_TOOLS = frozenset({"post_public_question", "finish_handoff"})

# Handed to the model when a turn produced prose instead of a tool call.
# Deliberately concrete: the text it just wrote is usually the right content
# for one of the two terminal tools, so the retry only has to route it.
_NUDGE = (
    "Your last reply was plain text, and plain text goes nowhere: the requester only ever sees messages sent "
    "with post_public_question, and the engineer only ever sees notes written with finish_handoff. Nobody read "
    "what you just wrote. Act now with a tool — if that text was meant for the requester, pass it as the "
    "`question` argument; if it was your summary of the ticket, pass it as the `note` argument."
)


@wrap_tool_call
async def _tool_gate(request, handler):
    """Turn a tool's refusal into feedback for the model.

    Only `ToolRejection` is caught. Real failures (iTop down, Redis down)
    propagate and fail the run. Invalid arguments never reach here: `ToolNode`
    converts them into an error `ToolMessage` on its own.
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


@wrap_model_call
async def _require_tool_call(request, handler):
    """Give a prose-only turn exactly one more chance to act.

    Observed in production on round 2: with a real conversation in the
    prompt, the model continues the *dialogue* — it writes the clarifying
    question as its answer instead of passing it to `post_public_question`.
    The text is then thrown away and the epilogue closes the ticket with a
    generic note, spending a round for nothing.

    The failed turn and the nudge stay in the message history on purpose —
    the model sees its own correction, and the journal shows the retry
    happened.

    This runs **even when `_force_tool_choice` is active**, and is not
    redundant there: forcing only helps where the endpoint honours it, and
    the ones that do not (Ollama, some OpenAI-compatible gateways) drop the
    field silently rather than erroring — the model still answers in prose,
    and this retry is the only thing that catches it. Which endpoints accept
    the forced choice is recorded in `llm_providers`.

    Retries are invisible to `ModelCallLimitMiddleware`: it counts model
    *node* executions (`after_model`), while this retry happens inside one.
    Hence exactly one — the worst case stays at 2 × `max_iterations` real
    requests, and the `usage` step counts them honestly.

    To see how often this fires, grep the journal for `agent` steps starting
    with `no tool call:`.
    """
    response = await handler(request)
    message = response.result[-1] if response.result else None
    if not isinstance(message, AIMessage) or message.tool_calls:
        return response

    logger.info("intake: model answered with plain text instead of a tool call, retrying once")
    nudge = HumanMessage(content=_NUDGE)
    retried = await handler(request.override(messages=[*request.messages, message, nudge]))
    return ModelResponse(
        result=[message, nudge, *retried.result],
        structured_response=retried.structured_response,
    )


def _force_tool_choice():
    """Leave the model no way to answer in prose: `tool_choice="any"` on every turn.

    Only for endpoints that accept it (`llm_providers`), and only for agents
    that want it. Intake does: its model's plain text reaches nobody, so a
    prose turn is always a mistake. An agent that answers the user directly
    must not add this middleware.

    Forcing on *every* turn — rather than only after a slip — is safe here
    because nothing in this agent needs a closing prose message:
    `_stop_after_terminal` ends the run before the next model call and
    `ModelCallLimitMiddleware` caps the rest. In an agent that must produce a
    final answer, the same middleware would loop until the budget runs out.

    Written as `"any"`, never `"required"`: LangChain normalizes per provider
    (`langchain_openai` maps `"any"` → `"required"` because OpenAI has no
    `"any"`), so the spelling survives a provider switch.
    """

    @wrap_model_call
    async def middleware(request, handler):
        return await handler(request.override(tool_choice="any"))

    return middleware


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


def build_intake_agent(
    llm: BaseChatModel,
    cfg: IntakeConfig,
    tools: list[BaseTool],
    force_tool_choice: bool = False,
) -> CompiledStateGraph:
    """Build the agent for one run. `tools` is the run's set — see `tools_for`.

    `force_tool_choice` says the endpoint accepts `tool_choice="any"` (the
    caller asks `LlmConfig.endpoint_forces_tool_choice`). Default off, so
    forcing is always a deliberate choice by the module building the agent.
    """
    middleware: list[Any] = [_tool_gate, _require_tool_call]
    if force_tool_choice:
        # Innermost of the two model wrappers: applies to the retry as well
        middleware.append(_force_tool_choice())
    middleware += [
        _stop_after_terminal,
        ModelCallLimitMiddleware(run_limit=cfg.max_iterations, exit_behavior="end"),
    ]
    return create_agent(model=llm, tools=tools, context_schema=IntakeContext, middleware=middleware)


def is_terminal_result(message: ToolMessage) -> bool:
    return message.name in TERMINAL_TOOLS and message.status == "success"


def describe_ai_message(message: AIMessage, think_tags: tuple[str, ...]) -> str:
    """One journal line for a model turn: which tools, with which arguments."""
    if message.tool_calls:
        calls = "; ".join(f"{call['name']}({call['args']})" for call in message.tool_calls)
        return f"calls: {calls}"
    # strip_thinking touches displayed text only — never tool-call arguments
    return f"no tool call: {strip_thinking(message.content, think_tags)[:300]}"
