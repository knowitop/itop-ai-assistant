"""Intake's own business rules (rule 2.1), pure and framework-free.

`tools.py` calls these and translates the outcome into I/O or a
`ToolRejection` — it does not compare thresholds or parse `.strip()` itself.
"""

from dataclasses import dataclass
from enum import Enum, auto

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
