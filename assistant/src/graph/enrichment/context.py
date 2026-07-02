from dataclasses import dataclass
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel

from config import EnrichmentConfig, TicketMappingConfig
from itop.repository import TicketRepository
from itop_client import Itop
from state.ticket_state import TicketStateManager

from .prompts import EnrichmentPrompts


@dataclass
class GraphContext:
    """Everything a single enrichment run needs.

    Built per run from AppDeps: `enrichment` and `prompts` are consistent
    snapshots for the whole run, LLM clients are created per run so per-node
    model overrides and future runtime config changes apply without restart.
    `ticket_repo` handles all semantic ticket operations; `itop_client` is for
    generic reads of other classes (Service, ServiceSubcategory).
    """

    processing_id: UUID
    itop_client: Itop
    ticket_repo: TicketRepository
    ticket_mapping: TicketMappingConfig
    state_manager: TicketStateManager
    enrichment: EnrichmentConfig
    prompts: EnrichmentPrompts
    llm_classify: BaseChatModel
    llm_evaluate: BaseChatModel
    llm_enrich: BaseChatModel
