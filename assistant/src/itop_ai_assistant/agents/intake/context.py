from dataclasses import dataclass
from uuid import UUID

from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.repositories.ticket import TicketRepository
from itop_ai_assistant.vector import SimilarSearch

from .config import IntakeConfig
from .domain import IntakeScope
from .state import IntakeState


@dataclass
class IntakeContext:
    """Everything a single intake run needs — the agent's `context_schema`.

    The ticket itself lives here: tools read and mutate it, and
    `set_classification` updates the snapshot after writing to iTop, so later
    tools in the same run see the new values. No LLM field — the model sits
    inside the agent.
    """

    processing_id: UUID
    # Who this run acts as. Here rather than only in `RunContext` because a
    # tool that searches has to name it (TASK-032): the vector index is global,
    # and what a search may return is decided by confirming its candidates
    # under this principal.
    principal: Principal
    ticket: Ticket
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    state_manager: IntakeState
    intake: IntakeConfig
    # What this run may do at all. The tool set, the prompt and the journal
    # read the composition of actions from here, never from `intake` — that
    # one stays the source of thresholds and texts.
    scope: IntakeScope
    ai_name: str
    # `similar` holds the subsystem's door (TASK-033), not a search assembled
    # for this run — None on a deployment without vectors, and the tool that
    # needs it is then not in the run's tool set at all (`tools_for`), so no
    # tool reads it expecting a value.
    similar: SimilarSearch | None = None
