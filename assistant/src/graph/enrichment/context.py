from dataclasses import dataclass
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel

from config import EnrichmentConfig
from itop_client import Itop
from state.ticket_state import TicketStateManager

from .prompts import EnrichmentPrompts


@dataclass
class GraphContext:
    """Everything a single enrichment run needs.

    Built per run from AppDeps: `enrichment` and `prompts` are consistent
    snapshots for the whole run, LLM clients are created per run so per-node
    model overrides and future runtime config changes apply without restart.
    """

    processing_id: UUID
    itop_client: Itop
    state_manager: TicketStateManager
    enrichment: EnrichmentConfig
    prompts: EnrichmentPrompts
    llm_classify: BaseChatModel
    llm_evaluate: BaseChatModel
    llm_enrich: BaseChatModel
