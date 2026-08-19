"""The iTop connection: one client, its rebuild and its lifetime.

`ItopConnection` owns section `itop` and knows nothing about mappings — those
configure repositories, which are a layer of their own
(`repositories/sets.py`, TASK-027).

This package is our code over the vendored `itop_client/`: the connection and
the provisioning of iTop-side triggers. Everything above it — anything that
returns a domain object — belongs to `repositories/`.
"""

import asyncio
from typing import Protocol

from itop_ai_assistant.config import ItopConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.repositories.identity import IdentityRepository
from itop_ai_assistant.settings.config_store import ConfigStore


class AiIdentity(Protocol):
    """Who the connection is, off its own (always service-account) client —
    never a delegated principal's — for the loop guard
    (`pipelines/shell.py`, `.claude/rules/core.md`). Declared next to
    `ItopConnection`, its one real implementation, so it can inherit it
    explicitly and hide `client()`/`as_principal()`/`aclose()` the way
    `LockPort` already hides the rest of `TicketStateManager` (ADR-022).
    """

    async def ai_person_name(self) -> str: ...


class ItopConnection(AiIdentity):
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
