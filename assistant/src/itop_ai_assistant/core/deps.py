from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from fastapi import Request
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from itop_ai_assistant.config import LlmConfig, Settings, VectorConfig
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
from itop_ai_assistant.util.redis_keyspace import days_to_seconds
from itop_ai_assistant.vector import (
    ChunkStore,
    IndexJournal,
    QdrantChunkStore,
    SimilarSearch,
    VectorSyncState,
    build_vector_sources,
)


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
    vector_search: SimilarSearch
    vector_sync: VectorSyncState
    vector_journal: IndexJournal
    # Would be `Callable[[VectorConfig], Sequence[VectorSource[Any]]]`;
    # `VectorSource` isn't on the facade yet (that's TASK-035), so this one
    # field stays `Any` at the boundary rather than deep-importing around it.
    vector_sources: Callable[[VectorConfig], Sequence[Any]]

    async def aclose(self) -> None:
        await self.itop_connection.aclose()
        await self.state_manager.aclose()
        await self.vector_store.aclose()


def get_deps(request: Request) -> "AppDeps":
    """The container, off the request — for `webhook`/`request`/`admin`, which
    hand it whole into the run core (`.claude/rules/core.md`: `AppDeps` goes to
    entry points and no deeper, and a module's `handle` is where it is taken
    apart into ports). A function that needs one field, not the container,
    belongs in `core/api_deps.py` instead — kept free of a real import of this
    module so `vector/router.py` can reuse it without reopening the facade
    cycle documented in `vector/__init__.py`.
    """
    return request.app.state.deps


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
    state_manager = TicketStateManager(redis, ttl_seconds=days_to_seconds(settings.state_ttl_days))
    itop_connection = ItopConnection(config_store)
    itop = ItopRepositories(itop_connection, config_store)
    # Lazy: no client (and no connection) until the vector store is used
    vector_store = QdrantChunkStore(settings.qdrant_url)

    # The one call site left for `build_vector_sources` (TASK-034) — closed
    # over `itop` here and handed to both `SimilarSearch` and `AppDeps`
    # itself, so `search.py`/`indexer.py`/`router.py` never import the
    # registry directly. Re-read `cfg.families` fresh on every call, not
    # collected once here: a static list would break the live reload TASK-021
    # relies on.
    def vector_sources(cfg: VectorConfig) -> list[Any]:
        return build_vector_sources(itop, cfg)

    return AppDeps(
        settings=settings,
        itop=itop,
        itop_connection=itop_connection,
        state_manager=state_manager,
        config_store=config_store,
        prompt_store=RedisPromptStore(FilePromptStore(PACKAGED_PROMPTS_DIR, settings.prompts_dir), redis),
        journal=RunJournal(redis, ttl_seconds=days_to_seconds(settings.run_ttl_days)),
        vector_store=vector_store,
        vector_search=SimilarSearch(vector_store, config_store, build_sources=vector_sources),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
        vector_sources=vector_sources,
    )
