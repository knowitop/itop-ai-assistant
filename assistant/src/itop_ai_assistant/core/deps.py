from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from itop_ai_assistant.config import LlmConfig, Settings
from itop_ai_assistant.core.llm_providers import get_provider
from itop_ai_assistant.itop.connection import ItopConnection
from itop_ai_assistant.repositories.sets import ItopRepositories
from itop_ai_assistant.settings.config_store import ConfigStore, RedisConfigStore
from itop_ai_assistant.settings.prompt_store import (
    PACKAGED_PROMPTS_DIR,
    FilePromptStore,
    PromptStore,
    RedisPromptStore,
)
from itop_ai_assistant.state.journal import RunJournal
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.vector.index_journal import IndexJournal
from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.store import ChunkStore
from itop_ai_assistant.vector.sync_state import VectorSyncState


@dataclass
class AppDeps:
    """Application-wide dependencies, assembled once at startup (composition root).

    Handed to entry points, and no deeper: the run core takes the narrow ports
    of `pipelines/ports.py` instead, and a module's handler is where this
    container is taken apart into them. `AppDeps` satisfies `RunDeps`
    structurally and deliberately does not inherit it — knowing about the ports
    would put the infrastructure imports above back into the core.

    `aclose()` is on purpose absent from every port: ownership of the connection
    pool stays here, so no run can be typed into closing it.
    """

    settings: Settings
    itop: ItopRepositories
    # The connection is held next to the factory built over it, not inside it:
    # closing the pool is the composition root's business, and the factory is
    # kept free of a lifecycle method for the same reason no port has one.
    itop_connection: ItopConnection
    state_manager: TicketStateManager
    config_store: ConfigStore
    prompt_store: PromptStore
    journal: RunJournal
    vector_store: ChunkStore
    vector_sync: VectorSyncState
    vector_journal: IndexJournal

    async def aclose(self) -> None:
        await self.itop_connection.aclose()
        await self.state_manager.aclose()
        await self.vector_store.aclose()


def create_llm(llm: LlmConfig, model: str | None = None) -> BaseChatModel:
    """Create an LLM client for the configured provider.

    `model` overrides the default `llm.model`; `llm.params` is forwarded
    verbatim to the provider's client (temperature, max_tokens, …).
    """
    provider = get_provider(llm.provider)
    kwargs: dict[str, Any] = dict(llm.params)
    if provider.base_url_mode != "unused" and llm.base_url:
        kwargs["base_url"] = llm.base_url
    if provider.api_key_mode == "required":
        kwargs["api_key"] = llm.api_key
    elif provider.api_key_mode == "optional":
        # Local endpoints (LM Studio) accept any key; the client requires one
        kwargs["api_key"] = llm.api_key or "unused"
    return init_chat_model(model or llm.model or "", model_provider=provider.langchain_provider, **kwargs)


def build_deps(settings: Settings) -> AppDeps:
    # One shared Redis connection pool for state, journal, config and prompts
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    config_store = RedisConfigStore(redis, settings)
    state_manager = TicketStateManager(redis, ttl_seconds=settings.state_ttl_days * 24 * 60 * 60)
    itop_connection = ItopConnection(config_store)
    return AppDeps(
        settings=settings,
        itop=ItopRepositories(itop_connection, config_store),
        itop_connection=itop_connection,
        state_manager=state_manager,
        config_store=config_store,
        prompt_store=RedisPromptStore(FilePromptStore(PACKAGED_PROMPTS_DIR, settings.prompts_dir), redis),
        journal=RunJournal(redis, ttl_seconds=settings.run_ttl_days * 24 * 60 * 60),
        # Lazy: no client (and no connection) until the vector store is used
        vector_store=QdrantChunkStore(settings.qdrant_url),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
    )
