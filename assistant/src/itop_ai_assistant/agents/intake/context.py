from dataclasses import dataclass
from uuid import UUID

from itop_ai_assistant.catalog_repository import CatalogRepository
from itop_ai_assistant.config import IntakeConfig
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.ticket_repository import TicketRepository


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
    state_manager: TicketStateManager
    intake: IntakeConfig
    ai_name: str
    think_tags: tuple[str, ...] = ("think", "thinking", "reasoning")
