"""Intake's own shape for the generic per-object state port.

`ObjectStatePort` (`pipelines/ports.py`) knows nothing about questions or
`ai_done` — it stores whatever model a caller hands it, under that caller's
own module name. `TicketState` is that model; `IntakeState` is the thin
adapter that binds `MODULE` and intake's field names once, so the rest of the
module (`tools.py`, `run.py`) speaks of `get`/`record_question`/`mark_done`
and never of field names.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from itop_ai_assistant.pipelines.ports import ObjectStatePort

from .prompts import MODULE


class TicketState(BaseModel):
    questions_asked: int = 0
    classify_questions_asked: int = 0
    ai_done: bool = False


@dataclass
class IntakeState:
    """Semantic AI-state operations for intake, over the generic port."""

    _store: ObjectStatePort

    async def get(self, ticket_ref: str) -> TicketState:
        return await self._store.get(MODULE, ticket_ref, TicketState)

    async def record_question(self, ticket_ref: str, *, classifying: bool) -> None:
        """One question to the requester, charged to the overall budget always.

        `questions_asked` counts messages to a human, and a classifying
        question is one of those — the phase sub-limit is spent on top of it,
        not instead. The two `increment` calls are not atomic: the port takes
        one field at a time on purpose (it knows nothing about this module's
        fields), and losing the second one to a Redis failure undercounts the
        phase sub-limit while the overall ceiling still holds.
        """
        await self._store.increment(MODULE, ticket_ref, "questions_asked")
        if classifying:
            await self._store.increment(MODULE, ticket_ref, "classify_questions_asked")

    async def mark_done(self, ticket_ref: str) -> None:
        await self._store.set_flag(MODULE, ticket_ref, "ai_done")
