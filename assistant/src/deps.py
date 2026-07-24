import asyncio
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as aioredis
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from catalog_repository import CatalogRepository
from config import ItopConfig, LlmConfig, Settings, TicketMappingConfig
from config_store import ConfigStore, RedisConfigStore
from itop_client import Itop
from journal import RunJournal
from prompt_store import FilePromptStore, PromptStore, RedisPromptStore
from state.ticket_state import TicketStateManager
from ticket_repository import TicketRepository
from vector.db import VectorDb

_DEFAULT_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"  # assistant/prompts


@dataclass
class ItopBundle:
    """iTop client plus the repositories bound to it — one consistent unit."""

    client: Itop
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository


class ItopProvider:
    """Serves the iTop client and repositories built from the effective runtime config.

    The bundle is cached and rebuilt (old client closed) whenever the "itop"
    or "ticket_mapping" section changes — connection edits made through the
    setup API apply from the next processed ticket without a restart. The
    per-process caches living inside the repositories (e.g. the AI person
    name) are dropped together with the bundle.
    """

    def __init__(self, config_store: ConfigStore):
        self._config_store = config_store
        self._bundle: ItopBundle | None = None
        self._fingerprint: str | None = None
        self._rebuild_lock = asyncio.Lock()

    async def get(self) -> ItopBundle:
        itop_cfg = await self._config_store.get("itop", ItopConfig)
        mapping = await self._config_store.get("ticket_mapping", TicketMappingConfig)
        fingerprint = itop_cfg.model_dump_json() + mapping.model_dump_json()
        async with self._rebuild_lock:
            if self._bundle is None or fingerprint != self._fingerprint:
                if self._bundle is not None:
                    await self._bundle.client.aclose()
                client = create_itop_client(itop_cfg)
                self._bundle = ItopBundle(
                    client=client,
                    ticket_repo=TicketRepository(client, mapping),
                    catalog_repo=CatalogRepository(client),
                )
                self._fingerprint = fingerprint
            return self._bundle

    async def aclose(self) -> None:
        if self._bundle is not None:
            await self._bundle.client.aclose()
            self._bundle = None
            self._fingerprint = None


@dataclass
class AppDeps:
    """Application-wide dependencies, assembled once at startup (composition root)."""

    settings: Settings
    itop: ItopProvider
    state_manager: TicketStateManager
    config_store: ConfigStore
    prompt_store: PromptStore
    journal: RunJournal
    vector_db: VectorDb

    async def aclose(self) -> None:
        await self.itop.aclose()
        await self.state_manager.aclose()
        await self.vector_db.aclose()


def create_itop_client(cfg: ItopConfig) -> Itop:
    return Itop(
        url=cfg.url,
        version=cfg.api_version,
        auth_user=cfg.user,
        auth_pwd=cfg.pwd,
        auth_token=cfg.token,
        timeout=cfg.timeout,
    )


def create_llm(llm: LlmConfig, model: str | None = None) -> ChatOpenAI:
    """Create an LLM client. `model` overrides the default `llm.model`."""
    # Local endpoints (LM Studio) accept any key; ChatOpenAI just requires one
    return ChatOpenAI(
        model=model or llm.model or "",
        api_key=SecretStr(llm.api_key or "unused"),
        base_url=llm.base_url,
    )


def build_deps(settings: Settings) -> AppDeps:
    # One shared Redis connection pool for state, journal, config and prompts
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    config_store = RedisConfigStore(redis, settings)
    state_manager = TicketStateManager(redis, ttl_seconds=settings.state_ttl_days * 24 * 60 * 60)
    return AppDeps(
        settings=settings,
        itop=ItopProvider(config_store),
        state_manager=state_manager,
        config_store=config_store,
        prompt_store=RedisPromptStore(FilePromptStore(_DEFAULT_PROMPTS_DIR, settings.prompts_dir), redis),
        journal=RunJournal(redis, ttl_seconds=settings.run_ttl_days * 24 * 60 * 60),
        # Lazy: no engine (and no connection) until the vector store is used
        vector_db=VectorDb(settings.database_url),
    )
