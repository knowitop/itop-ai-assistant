"""What every run carries with it, from the entry point down to the iTop call.

Kept out of `models.py` on purpose: that file is what a run is started with and
ends as, and it is pydantic because `ObjectRef` serves as a route's input model.
A run context is never serialized — it is passed, not posted.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RunContext:
    """One run: which run it is and which module owns it.

    Replaces the bare `processing_id` at every handler boundary. A module name
    travelled here already — the frame took it as an argument — but the shell
    never saw it, so anything wanting to say "this was intake" had to be handed
    it separately.
    """

    processing_id: UUID
    module: str
