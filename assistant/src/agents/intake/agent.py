"""The intake agent: `create_agent` plus three middleware.

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


@wrap_model_call
async def _require_tool_call(request, handler):
    """Give a prose-only turn exactly one more chance to act.

    Observed in production on round 2: with a real conversation in the
    prompt, the model continues the *dialogue* — it writes the clarifying
    question as its answer instead of passing it to `post_public_question`.
    The text is then thrown away and the epilogue closes the ticket with a
    generic note, spending a round for nothing. The graph never has this
    failure mode: its nodes post the question themselves.

    The failed turn and the nudge stay in the message history on purpose —
    the model sees its own correction, and the journal shows the retry
    happened, which is exactly the kind of thing the A/B is measuring.

    **The stronger lever, deliberately not pulled yet:
    `request.override(tool_choice="any")`.** `ModelRequest` carries a
    `tool_choice` field, and forcing it would make prose impossible rather
    than merely correctable — semantically right, since this agent has no
    channel through which plain text could reach anyone. Two reasons it is
    not the default: an endpoint may not honour a forced choice (DeepSeek's
    API does, LM Studio / Ollama builds vary, and `llm.base_url` is
    runtime-editable), and a rejected `tool_choice` fails the whole run
    instead of degrading. If the journal shows nudges on a noticeable share
    of runs — grep the steps for `agent` details starting with
    `no tool call:` — force it, either on every turn or only on the retry
    with a fallback to this nudge when the endpoint errors.

    Retries are invisible to `ModelCallLimitMiddleware`: it counts model
    *node* executions (`after_model`), while this retry happens inside one.
    Hence exactly one — the worst case stays at 2 × `max_iterations` real
    requests, and the `usage` step counts them honestly.
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
            _require_tool_call,
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
