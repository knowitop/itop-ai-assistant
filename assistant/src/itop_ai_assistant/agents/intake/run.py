"""Intake's scenario: what the module does with one ticket.

Deterministic shell, agentic core — the shell itself is the core's
(`pipelines/shell.py`, `pipelines/agent_run.py`). What this module supplies is
intake's own: the guard's three predicates, the body that runs the agent
`compose.py` assembles, and what to write when the agent closed nothing
itself. Registration and assembly live elsewhere (`pipeline.py`,
`compose.py`) — this file is only ever about one ticket already in hand.
"""

import logging
from typing import Protocol

from itop_ai_assistant.domain.identity import ObjectIdentity
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.agent_run import AgentRun
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.ports import ObjectStatePort
from itop_ai_assistant.pipelines.shell import TicketRun

from . import compose, domain
from .agent import TERMINAL_TOOLS
from .config import IntakeConfig
from .context import IntakeContext
from .prompts import MODULE
from .state import IntakeState

logger = logging.getLogger(__name__)


class IntakeRun(TicketRun):
    """Ticket created or user commented: classify, ask, hand off."""

    async def stop_reason(self, ticket: Ticket, ai_name: str) -> str | None:
        """Read the AI state and config, then let `domain.stop_reason` decide."""
        ticket_state = await IntakeState(self.deps.state_manager).get(str(ticket.identity))
        cfg = await self.deps.config_store.get(MODULE, IntakeConfig)
        return domain.stop_reason(ticket, ticket_state, active_statuses=cfg.active_statuses, ai_name=ai_name)

    async def body(self, ticket: Ticket, ai_name: str) -> None:
        logger.info(f"[{self.processing_id}] Running intake agent for {ticket.identity}")

        composed = await compose.assemble(ticket, ai_name, self.run, self.deps, self.repos)
        await IntakeAgentRun(
            composed.agent,
            composed.context,
            journal=self.journal,
            run=self.run,
            think_tags=composed.think_tags,
        ).stream(composed.messages)


class IntakeAgentRun(AgentRun[IntakeContext]):
    """The intake agent loop: one question or one handoff, never both."""

    terminal_tools = TERMINAL_TOOLS

    async def epilogue(self) -> None:
        """Close a run the agent did not close itself.

        The model answered in plain text, burned `max_iterations` or looped.
        Setting `ai_done` is mandatory: otherwise the next webhook replays the
        whole expensive cycle to the same end.

        The `ai_done` re-read guards a real race: `handle_assigned` takes no
        lock, so an engineer can pick the ticket up while the agent is still
        thinking — and a fallback note posted after that would land on someone
        else's ticket.
        """
        ticket = self.context.ticket
        state = await self.context.state_manager.get(str(ticket.identity))
        if state.ai_done:
            await self.step("epilogue", "ticket already finished — nothing to close")
            return

        logger.info(
            f"[{self.processing_id}] {ticket.identity}: agent produced no terminal action, posting fallback note"
        )
        await self.context.ticket_repo.append_private_log(ticket, self.context.intake.handoff_fallback_note)
        await self.context.state_manager.mark_done(str(ticket.identity))
        await self.step("epilogue", "no terminal action — fallback note posted, ai_done set")


class _AssignedDeps(Protocol):
    """All `handle_assigned` is allowed to reach.

    Wider than `RunDeps` in the type-system sense, and that is what makes it
    legal as a handler: a parameter type is contravariant, so a handler asking
    for less still fits a registry that hands it more.
    """

    @property
    def state_manager(self) -> ObjectStatePort: ...


async def handle_assigned(ref: ObjectIdentity, run: RunContext, deps: _AssignedDeps) -> None:
    """Engineer took the ticket: stop any further AI processing.

    Not a `TicketRun`: no lock and no read. The missing lock is deliberate — it
    is what lets this land while an agent is mid-run, and what
    `IntakeAgentRun.epilogue` re-reads `ai_done` for.

    Typed by the narrowest thing that still fits the registry's handler shape:
    this route marks one ticket done and has no business reaching iTop, the
    model or the vector store.
    """
    await IntakeState(deps.state_manager).mark_done(str(ref))
    logger.info(f"[{run.processing_id}] {ref} assigned, marked done")
