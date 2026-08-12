"""The agent half of the run shell: one agent loop, its journal trace and its
guaranteed closure.

Kept separate from `shell.TicketRun` because the two halves have different
consumers. A webhook module needs both. A synchronous module — the engineer
console — needs this one and no lock at all: it holds no state on the ticket and
answers its caller directly. Composition keeps that possible; a single template
method would force the console to stub the locking half out.
"""

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, ClassVar, Generic, TypeVar
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.ports import StepJournal
from itop_ai_assistant.text_utils import strip_thinking

logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")


@dataclass
class RunUsage:
    """Token and call accounting for one agent run."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, message: AIMessage) -> None:
        self.model_calls += 1
        metadata = message.usage_metadata or {}
        self.input_tokens += metadata.get("input_tokens", 0)
        self.output_tokens += metadata.get("output_tokens", 0)

    def describe(self, elapsed: float) -> str:
        return (
            f"model calls: {self.model_calls}; "
            f"tokens in/out: {self.input_tokens}/{self.output_tokens}; "
            f"wall time: {elapsed:.1f}s"
        )


def describe_ai_message(message: AIMessage, think_tags: tuple[str, ...]) -> str:
    """One journal line for a model turn: which tools, with which arguments."""
    if message.tool_calls:
        calls = "; ".join(f"{call['name']}({call['args']})" for call in message.tool_calls)
        return f"calls: {calls}"
    # strip_thinking touches displayed text only — never tool-call arguments
    return f"no tool call: {strip_thinking(message.content, think_tags)[:300]}"


class AgentRun(Generic[ContextT]):
    """One agent loop: journalled turn by turn, counted, and closed for sure.

    `terminal_tools` names the tools whose success ends the session. If none of
    them succeeded by the time the stream ends, `epilogue()` runs — that is the
    guarantee the shell owes every module: the model may answer in prose, burn
    its iteration budget or loop, and the run still closes instead of being
    replayed in full by the next event.
    """

    terminal_tools: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        agent: CompiledStateGraph,
        context: ContextT,
        *,
        journal: StepJournal,
        run: RunContext,
        think_tags: tuple[str, ...],
    ) -> None:
        self.agent = agent
        self.context = context
        # Steps only: the agent loop records inside a frame the entry point
        # opened, and must not be able to open or finish a run itself.
        self.journal = journal
        self.run = run
        self.think_tags = think_tags

    @property
    def processing_id(self) -> UUID:
        return self.run.processing_id

    async def stream(self, messages: list[BaseMessage]) -> RunUsage:
        """Run the agent to the end, journalling every turn. Returns what it cost."""
        usage = RunUsage()
        started = perf_counter()
        try:
            terminal_done = False
            async for update in self.agent.astream({"messages": messages}, context=self.context, stream_mode="updates"):
                terminal_done |= await self._journal_update(update, usage)

            if not terminal_done:
                await self.epilogue()
        finally:
            # What this run actually cost. Recorded even when the run failed — a
            # burnt budget is data too.
            await self.step("usage", usage.describe(perf_counter() - started))
        return usage

    def is_terminal(self, message: ToolMessage) -> bool:
        return message.name in self.terminal_tools and message.status == "success"

    async def epilogue(self) -> None:
        """Close a run the agent did not close itself. Default: nothing to close."""
        return None

    async def step(self, node: str, detail: str = "") -> None:
        await self.journal.add_step(self.processing_id, node, detail)

    async def _journal_update(self, update: dict[str, Any], usage: RunUsage) -> bool:
        """Turn one stream update into journal steps; report a terminal tool result.

        Journalling happens here rather than inside the tools: what needs
        recording is the model's *decisions* — which tool it picked, with which
        arguments, what it wrote as plain text — and that lives in the AIMessage,
        which a tool cannot see. Journal writes are non-fatal by contract; from
        inside a tool a failure would surface to the model as a tool error.
        """
        terminal = False
        for output in update.values():
            if not isinstance(output, dict):
                continue
            for message in output.get("messages") or []:
                if isinstance(message, AIMessage):
                    usage.add(message)
                    await self.step("agent", describe_ai_message(message, self.think_tags))
                elif isinstance(message, ToolMessage):
                    detail = f"[{message.status}] {message.text[:300]}"
                    # `artifact` never reaches the model (unlike `content`) — a
                    # tool built with response_format="content_and_artifact"
                    # uses it to hand the journal something more precise than
                    # its own truncated prose (TASK-014).
                    if isinstance(message.artifact, str) and message.artifact:
                        detail = f"{detail} | {message.artifact}"
                    await self.step(f"tool:{message.name}", detail)
                    terminal |= self.is_terminal(message)
        return terminal
