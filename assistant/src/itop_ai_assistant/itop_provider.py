"""The iTop connection and the repositories bound to it.

Kept out of `deps.py` deliberately. `deps` is the composition root: it imports
Redis, Qdrant and langchain, so anything that imports it inherits the whole
infrastructure. The run shell needs to *name* this provider in a type, and
nothing else from the container — importing `deps` for that was what forced the
`TYPE_CHECKING` dance in `pipelines/`.

`deps.py` still assembles this; it just no longer owns the definition.
"""

import asyncio
from dataclasses import dataclass

from itop_ai_assistant.access_repository import AccessRepository
from itop_ai_assistant.catalog_repository import CatalogRepository
from itop_ai_assistant.config import FaqMappingConfig, ItopConfig, TicketMappingConfig
from itop_ai_assistant.config_store import ConfigStore
from itop_ai_assistant.faq_repository import FaqRepository
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.principal import Principal
from itop_ai_assistant.ticket_repository import TicketRepository


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
