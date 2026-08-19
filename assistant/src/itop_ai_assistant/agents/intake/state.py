"""Intake's own shape for the generic per-object state port.

`ObjectStatePort` (`pipelines/ports.py`) knows nothing about rounds or
`ai_done` — it stores whatever model a caller hands it, under that caller's
own module name. `TicketState` is that model; `IntakeState` is the thin
adapter that binds `MODULE` and intake's field names once, so the rest of the
module (`tools.py`, `run.py`) keeps calling `get`/`increment_rounds`/
`increment_classify_rounds`/`mark_done` exactly as before.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from itop_ai_assistant.pipelines.ports import ObjectStatePort

from .prompts import MODULE


class TicketState(BaseModel):
    rounds: int = 0
    classify_rounds: int = 0
    ai_done: bool = False


@dataclass
class IntakeState:
    """Semantic AI-state operations for intake, over the generic port."""

    _store: ObjectStatePort

    async def get(self, ticket_ref: str) -> TicketState:
        return await self._store.get(MODULE, ticket_ref, TicketState)

    async def increment_rounds(self, ticket_ref: str) -> None:
        await self._store.increment(MODULE, ticket_ref, "rounds")

    async def increment_classify_rounds(self, ticket_ref: str) -> None:
        await self._store.increment(MODULE, ticket_ref, "classify_rounds")

    async def mark_done(self, ticket_ref: str) -> None:
        await self._store.set_flag(MODULE, ticket_ref, "ai_done")
