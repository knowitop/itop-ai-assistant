"""The iTop connection, and the repository sets built over it.

Two collaborators and one record, split along who owns which config section
(TASK-027). `ItopConnection` owns section `itop` — the client, its rebuild and
its lifetime — and knows nothing about mappings. `ItopRepositories` owns
`ticket_mapping` / `faq_mapping`, because a repository is what they configure,
and is the only place where repositories are listed. `RepositorySet` is the
record a run holds; it creates nothing.

Kept out of `deps.py` deliberately. `deps` is the composition root: it imports
Redis, Qdrant and langchain, so anything that imports it inherits the whole
infrastructure. The run shell needs to *name* these types and nothing else from
the container — importing `deps` for that was what forced the `TYPE_CHECKING`
dance in `pipelines/` (ADR-019).
"""

import asyncio
from dataclasses import dataclass

from itop_ai_assistant.access_repository import AccessRepository
from itop_ai_assistant.catalog_repository import CatalogRepository
from itop_ai_assistant.config import FaqMappingConfig, ItopConfig, TicketMappingConfig
from itop_ai_assistant.config_store import ConfigStore
from itop_ai_assistant.faq_repository import FaqRepository
from itop_ai_assistant.identity_repository import IdentityRepository
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.principal import Principal
from itop_ai_assistant.ticket_repository import TicketRepository


class ItopConnection:
    """One iTop connection, rebuilt when — and only when — its own section changes.

    The client is cached by the fingerprint of section `itop`; a connection edit
    made through the setup API applies from the next processed ticket without a
    restart. Mapping sections are deliberately not in that fingerprint: they
    configure repositories, and rebuilding the client for them would close a
    pool shared with every principal view over nothing the connection cares
    about.
    """

    def __init__(self, config_store: ConfigStore):
        self._config_store = config_store
        self._client: Itop | None = None
        self._fingerprint: str | None = None
        self._ai_person_name: str | None = None
        self._rebuild_lock = asyncio.Lock()

    async def client(self) -> Itop:
        """The service-account client for the effective config."""
        cfg = await self._config_store.get("itop", ItopConfig)
        fingerprint = cfg.model_dump_json()
        async with self._rebuild_lock:
            if self._client is None or fingerprint != self._fingerprint:
                if self._client is not None:
                    await self._client.aclose()
                self._client = create_itop_client(cfg)
                self._fingerprint = fingerprint
                self._ai_person_name = None
            return self._client

    async def as_principal(self, principal: Principal, *, comment: str) -> Itop:
        """The same connection, seen as this principal.

        A view, not a second client: iTop authenticates per request, so one pool
        legitimately serves several identities (`Itop.as_`). Every request the
        view makes — including the ones `Schema` issues on its own — inherits
        these credentials, which a per-call argument could not guarantee.
        """
        return (await self.client()).as_(auth=principal.auth, comment=comment)

    async def ai_person_name(self) -> str:
        """Friendly name of the AI service account. Cached until the client is rebuilt.

        Answered off the connection's own client whatever a run acts as, and
        reachable only from here: this name is what tells a run "the last public
        comment is our own" (`IntakeRun.stop_reason`), so resolving it under an
        engineer's token would turn the loop guard into a lie. That is also why
        `IdentityRepository` is built here instead of joining `RepositorySet`.
        """
        client = await self.client()
        if self._ai_person_name is None:
            self._ai_person_name = await IdentityRepository(client).current_person_name()
        return self._ai_person_name

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._fingerprint = None
            self._ai_person_name = None


@dataclass(frozen=True)
class RepositorySet:
    """The repositories of one principal — a record, not a factory.

    One set, one identity: everything in here talks to iTop with the same
    credentials, so a run cannot half-act as somebody else. The client is not a
    field, and there is no accessor for it: nothing outside the repositories
    touches iTop (`.claude/rules/itop.md`).
    """

    ticket_repo: TicketRepository
    catalog_repo: CatalogRepository
    access_repo: AccessRepository
    faq_repo: FaqRepository


class ItopRepositories:
    """Builds repository sets over a connection. The only place they are listed.

    Owns the mapping sections and reads them per set, so an admin edit applies
    from the next run with no client rebuild and no cache to invalidate — the
    repositories themselves are stateless, a client and a mapping and nothing
    else.
    """

    def __init__(self, connection: ItopConnection, config_store: ConfigStore):
        self._connection = connection
        self._config_store = config_store

    async def service(self) -> RepositorySet:
        """The set for the service account, on the bare connection.

        Deliberately not `for_principal(Principal.service())`: that would attach
        a change comment where there is none today. A bare call reads as the
        statement it is — no run, no principal, no writes (the vector sweep, the
        wizard probes, `selfcheck`).
        """
        return await self._build(await self._connection.client())

    async def for_principal(self, principal: Principal, *, comment: str) -> RepositorySet:
        """The set a run uses: one connection view, one identity, one comment."""
        return await self._build(await self._connection.as_principal(principal, comment=comment))

    async def ai_person_name(self) -> str:
        return await self._connection.ai_person_name()

    async def _build(self, client: Itop) -> RepositorySet:
        ticket_mapping = await self._config_store.get("ticket_mapping", TicketMappingConfig)
        faq_mapping = await self._config_store.get("faq_mapping", FaqMappingConfig)
        return RepositorySet(
            ticket_repo=TicketRepository(client, ticket_mapping),
            catalog_repo=CatalogRepository(client),
            access_repo=AccessRepository(client),
            faq_repo=FaqRepository(client, faq_mapping),
        )


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
