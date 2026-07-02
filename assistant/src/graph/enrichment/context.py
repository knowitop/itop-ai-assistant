from dataclasses import dataclass
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel

from catalog_repository import CatalogRepository
from config import EnrichmentConfig, TicketMappingConfig
from state.ticket_state import TicketStateManager
from ticket_repository import TicketRepository

from .prompts import EnrichmentPrompts


@dataclass
class GraphContext:
    """Everything a single enrichment run needs.

    Built per run from AppDeps: `enrichment` and `prompts` are consistent
    snapshots for the whole run, LLM clients are created per run so per-node
    model overrides and future runtime config changes apply without restart.
    All iTop access goes through the semantic repositories — nodes never see
    the raw iTop client.
    """

    processing_id: UUID
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    ticket_mapping: TicketMappingConfig
    state_manager: TicketStateManager
    enrichment: EnrichmentConfig
    prompts: EnrichmentPrompts
    llm_classify: BaseChatModel
    llm_evaluate: BaseChatModel
    llm_enrich: BaseChatModel
    think_tags: tuple[str, ...] = ("think", "thinking", "reasoning")
