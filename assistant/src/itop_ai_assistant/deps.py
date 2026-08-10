import asyncio
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from itop_ai_assistant.access_repository import AccessRepository
from itop_ai_assistant.catalog_repository import CatalogRepository
from itop_ai_assistant.config import FaqMappingConfig, ItopConfig, LlmConfig, Settings, TicketMappingConfig
from itop_ai_assistant.config_store import ConfigStore, RedisConfigStore
from itop_ai_assistant.faq_repository import FaqRepository
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.journal import RunJournal
from itop_ai_assistant.llm_providers import get_provider
from itop_ai_assistant.principal import Principal
from itop_ai_assistant.prompt_store import (
    PACKAGED_PROMPTS_DIR,
    FilePromptStore,
    PromptStore,
    RedisPromptStore,
)
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.ticket_repository import TicketRepository
from itop_ai_assistant.vector.index_journal import IndexJournal
from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.store import ChunkStore
from itop_ai_assistant.vector.sync_state import VectorSyncState


@dataclass
class ItopBundle:
    """iTop client plus the repositories bound to it — one consistent unit.

    One connection seen as one principal: everything in here talks to iTop with
    the same credentials, so a run cannot half-act as somebody else.
    """

    client: Itop
    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    access_repo: AccessRepository
    faq_repo: FaqRepository


class ItopProvider:
    """Serves the iTop client and repositories built from the effective runtime config.

    The bundle is cached and rebuilt (old client closed) whenever the "itop",
    "ticket_mapping" or "faq_mapping" section changes — connection edits made
    through the setup API apply from the next processed ticket without a
    restart. The per-process caches living inside the repositories (e.g. the
    AI person name) are dropped together with the bundle.
    """

    def __init__(self, config_store: ConfigStore):
        self._config_store = config_store
        self._bundle: ItopBundle | None = None
        self._fingerprint: str | None = None
        self._ai_person_name: str | None = None
        self._rebuild_lock = asyncio.Lock()

    async def get(self) -> ItopBundle:
        itop_cfg = await self._config_store.get("itop", ItopConfig)
        ticket_mapping = await self._config_store.get("ticket_mapping", TicketMappingConfig)
        faq_mapping = await self._config_store.get("faq_mapping", FaqMappingConfig)
        fingerprint = itop_cfg.model_dump_json() + ticket_mapping.model_dump_json() + faq_mapping.model_dump_json()
        async with self._rebuild_lock:
            if self._bundle is None or fingerprint != self._fingerprint:
                if self._bundle is not None:
                    await self._bundle.client.aclose()
                client = create_itop_client(itop_cfg)
                self._bundle = ItopBundle(
                    client=client,
                    ticket_repo=TicketRepository(client, ticket_mapping),
                    catalog_repo=CatalogRepository(client),
                    access_repo=AccessRepository(client),
                    faq_repo=FaqRepository(client, faq_mapping),
                )
                self._fingerprint = fingerprint
                self._ai_person_name = None
            return self._bundle

    async def ticket_repo(self) -> TicketRepository:
        """The plain-connection `ticket_repo` — narrower than `get()` for a
        caller that needs only this one repository, not the whole bundle."""
        return (await self.get()).ticket_repo

    async def faq_repo(self) -> FaqRepository:
        """Same narrowing as `ticket_repo()`, for `FaqRepository`."""
        return (await self.get()).faq_repo

    async def for_principal(self, principal: Principal, *, comment: str) -> ItopBundle:
        """The same connection, seen as this principal. One run, one bundle.

        Deliberately a second method rather than a defaulted argument on
        `get()`: "I forgot to say who is acting" would be invisible with a
        default, whereas a plain `get()` at a call site now reads as the
        statement it is — no run, no principal, the service account.

        Adds no cache of its own. The connection is cached by the fingerprint of
        its config sections, and the view over it costs three small objects, no
        HTTP client and no lock. Repositories are rebuilt per run so that
        nothing in them can reach the service credentials by accident.
        """
        base = await self.get()
        client = base.client.as_(auth=principal.auth, comment=comment)
        if client is base.client:
            return base
        return ItopBundle(
            client=client,
            ticket_repo=TicketRepository(client, base.ticket_repo.mapping),
            catalog_repo=CatalogRepository(client),
            access_repo=AccessRepository(client),
            faq_repo=FaqRepository(client, base.faq_repo.mapping),
        )

    async def ai_person_name(self) -> str:
        """Friendly name of the AI service account. Cached until the bundle is rebuilt.

        A property of the connection, not of the ticket repository: it maps
        nothing, it asks iTop who the service account is. It also has to stay
        that way — the name is what tells a run "this last comment is our own"
        (`IntakeRun.stop_reason`), so resolving it as anyone else would turn the
        loop guard into a lie. Answering it here, off the service bundle, makes
        that impossible rather than merely unlikely.
        """
        bundle = await self.get()
        if self._ai_person_name is None:
            person = await bundle.client.schema("Person").find_one({"id": ("=", ":current_contact_id")})
            if person is None:
                # Reachable state — the setup wizard probes for exactly this.
                raise ValueError("No Person is linked to the iTop service account")
            self._ai_person_name = person["friendlyname"]
        return self._ai_person_name

    async def aclose(self) -> None:
        if self._bundle is not None:
            await self._bundle.client.aclose()
            self._bundle = None
            self._fingerprint = None
            self._ai_person_name = None


@dataclass
class AppDeps:
    """Application-wide dependencies, assembled once at startup (composition root)."""

    settings: Settings
    itop: ItopProvider
    state_manager: TicketStateManager
    config_store: ConfigStore
    prompt_store: PromptStore
    journal: RunJournal
    vector_store: ChunkStore
    vector_sync: VectorSyncState
    vector_journal: IndexJournal

    async def aclose(self) -> None:
        await self.itop.aclose()
        await self.state_manager.aclose()
        await self.vector_store.aclose()


def create_itop_client(cfg: ItopConfig) -> Itop:
    # Callers reach this only past `missing_setup()` (webhooks) or past the
    # wizard's own check (setup probes) — an unset URL here is a bug, and
    # failing now beats a confusing httpx error on the first request.
    if not cfg.url:
        raise ValueError("iTop URL is not configured")
    return Itop(
        url=cfg.url,
        version=cfg.api_version,
        auth_user=cfg.user,
        auth_pwd=cfg.pwd,
        auth_token=cfg.token,
        timeout=cfg.timeout,
    )


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
    return AppDeps(
        settings=settings,
        itop=ItopProvider(config_store),
        state_manager=state_manager,
        config_store=config_store,
        prompt_store=RedisPromptStore(FilePromptStore(PACKAGED_PROMPTS_DIR, settings.prompts_dir), redis),
        journal=RunJournal(redis, ttl_seconds=settings.run_ttl_days * 24 * 60 * 60),
        # Lazy: no client (and no connection) until the vector store is used
        vector_store=QdrantChunkStore(settings.qdrant_url),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
    )
