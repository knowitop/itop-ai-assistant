from dataclasses import dataclass
from uuid import UUID

from itop_ai_assistant.config import IntakeConfig
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.ports import TicketStatePort
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.repositories.ticket import TicketRepository
from itop_ai_assistant.vector.search import SimilarSearch


@dataclass
class IntakeContext:
    """Everything a single intake run needs — the agent's `context_schema`.

    The ticket itself lives here: tools read and mutate it, and
    `set_classification` updates the snapshot after writing to iTop, so later
    tools in the same run see the new values. No LLM field — the model sits
    inside the agent.
    """

    processing_id: UUID
    ticket: Ticket
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    state_manager: TicketStatePort
    intake: IntakeConfig
    ai_name: str
    # `similar` is None on a deployment without vectors — the tool that needs
    # it is then not in the run's tool set at all (`tools_for`), so no tool
    # reads it expecting a value.
    similar: SimilarSearch | None = None
