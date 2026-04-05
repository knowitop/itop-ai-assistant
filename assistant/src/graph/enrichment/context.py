from dataclasses import dataclass
from uuid import UUID

from itop_client import Itop
from state.ticket_state import TicketStateManager


@dataclass
class GraphContext:
    processing_id: UUID
    itop_client: Itop
    state_manager: TicketStateManager
