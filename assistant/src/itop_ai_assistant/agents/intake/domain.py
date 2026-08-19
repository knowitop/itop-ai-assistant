"""Intake's own business rules (rule 2.1), pure and framework-free.

`tools.py` and `run.py` call these and translate the outcome into I/O, a
`ToolRejection` or a skip — they do not compare thresholds, parse `.strip()`
or inspect the ticket/log themselves.
"""

from dataclasses import dataclass
from enum import Enum, auto

from itop_ai_assistant.domain.ticket import Ticket

from .state import TicketState


class RoundBudget(Enum):
    """What `post_public_question` may do, given the rounds spent so far."""

    OK = auto()  # under budget — post the question
    CLASSIFY_EXHAUSTED = auto()  # asked twice while classifying — hand off with the fallback note
    EXHAUSTED = auto()  # asked twice after classification — refuse, point at finish_handoff


def check_round_budget(
    state: TicketState, *, classifying: bool, max_rounds: int, max_classify_rounds: int
) -> RoundBudget:
    if classifying:
        return RoundBudget.CLASSIFY_EXHAUSTED if state.classify_rounds >= max_classify_rounds else RoundBudget.OK
    return RoundBudget.EXHAUSTED if state.rounds >= max_rounds else RoundBudget.OK


@dataclass(frozen=True)
class NonBlankText:
    """A question or a note is not an answer if there is nothing in it."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("text is blank")


def stop_reason(ticket: Ticket, state: TicketState, *, active_statuses: list[str], ai_name: str) -> str | None:
    """Why this ticket must not be processed, or None to proceed."""
    if state.ai_done:
        return "already processed (ai_done)"
    if ticket.status not in active_statuses:
        return f"status={ticket.status} not in {active_statuses}"
    # Loop protection, second line of defense after iTop trigger contexts:
    # if our own question is the last public entry, wait for the user instead
    # of reacting to our own comment or a duplicate webhook.
    if ticket.public_log and ticket.public_log[-1].user_login == ai_name:
        return "last public entry is ours, waiting for the requester"
    return None
