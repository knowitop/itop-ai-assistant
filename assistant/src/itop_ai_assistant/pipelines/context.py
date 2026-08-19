"""What every run carries with it, from the entry point down to the iTop call.

Kept out of `models.py` on purpose: that file is what a run ends as, and
`domain.identity.ObjectIdentity` — what a run is *started* with — is pydantic
because it serves as a route's input model. A run context is never serialized
— it is passed, not posted.
"""

from dataclasses import dataclass, field
from uuid import UUID

from itop_ai_assistant.core.principal import Principal


@dataclass(frozen=True)
class RunContext:
    """One run: which run it is, which module owns it and who it acts as.

    Replaces the bare `processing_id` at every handler boundary. A module name
    travelled here already — the frame took it as an argument — but the shell
    never saw it, so anything wanting to say "this was intake" had to be handed
    it separately.
    """

    processing_id: UUID
    module: str
    principal: Principal = field(default_factory=Principal.service)

    @property
    def comment(self) -> str:
        """What iTop records in the object's History for every write this run makes.

        Under a delegated principal the account is the engineer's own, so
        nothing in iTop would otherwise distinguish what they did from what the
        assistant did for them. The run id is the one the Runs screen searches
        by: an odd change in the History leads straight to the trace of the run
        that made it.
        """
        text = f"AI assistant · {self.module} · run {self.processing_id}"
        if self.principal.on_behalf_of:
            text += f" · on behalf of {self.principal.on_behalf_of}"
        return text
