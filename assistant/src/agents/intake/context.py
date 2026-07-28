from dataclasses import dataclass
from uuid import UUID

from catalog_repository import CatalogRepository
from config import IntakeConfig
from domain.ticket import Ticket
from state.ticket_state import TicketStateManager
from ticket_repository import TicketRepository


@dataclass
class IntakeContext:
    """Everything a single intake run needs — the agent's `context_schema`.

    Mirror of the enrichment `GraphContext`, with two differences: the ticket
    itself lives here (tools read and mutate it — `set_classification` updates
    the snapshot after writing to iTop), and there is no LLM field: the model
    sits inside the agent.
    """

    processing_id: UUID
    ticket: Ticket
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    state_manager: TicketStateManager
    intake: IntakeConfig
    ai_name: str
    think_tags: tuple[str, ...] = ("think", "thinking", "reasoning")
