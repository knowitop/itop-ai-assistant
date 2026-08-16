"""Assembles the list of VectorSource instances the indexer sweeps.

Adding a new source: create `vector/sources/<name>.py` implementing
`vector.ports.source.VectorSource`, and add one line to `_BUILDERS` below —
same pattern as `pipelines/registry.py` for webhook modules.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from itop_ai_assistant.config import FamilyConfig, VectorConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.repositories.faq import FaqRepository
from itop_ai_assistant.repositories.sets import RepositorySet
from itop_ai_assistant.repositories.ticket import TicketRepository

if TYPE_CHECKING:
    from itop_ai_assistant.vector.ports.source import VectorSource

logger = logging.getLogger(__name__)

# Neither is a write today — sweep and confirm are both reads — but
# `for_principal` requires a comment, and this subsystem has no run to name.
_SWEEP_COMMENT = "AI assistant · vector · sweep"
_CONFIRM_COMMENT = "AI assistant · vector · confirming search candidates"


class ItopRepos(Protocol):
    """How a source reaches iTop — declared here, by the consumer, rather than
    imported from the container that happens to offer it.

    A source needs two identities, not one (TASK-032): the sweep reads as the
    service account, since the index is global and built once for everyone;
    confirming a search candidate reads as whoever is asking, since that is
    the only honest answer to "may this person see it" (ADR-003). What keeps
    them from being mixed up is not this protocol but what
    `build_vector_sources` hands each source: two accessors, one per
    identity, and no way to reach `for_principal` itself.
    """

    async def for_principal(self, principal: Principal, *, comment: str) -> RepositorySet: ...


def build_vector_sources(itop: ItopRepos, cfg: VectorConfig) -> list["VectorSource[Any]"]:
    """One instance per *registered* family, not per family the saved config
    happens to still mention (TASK-021).

    Every known family is built unconditionally, with `classes` taken from
    `cfg.families.get(name, FamilyConfig()).classes` — empty if the admin
    cleared it or the key is missing entirely. That is what lets the admin UI
    (`GET /api/vector/sources`) always show a family's full chunking
    vocabulary, including recovering a class that was removed by mistake:
    before this, a family absent from the saved config had no vocabulary to
    offer at all, because this function only built what the config already
    contained.

    A `cfg.families` key that matches no builder below is logged and
    skipped — the family name is not something the admin can invent from the
    UI, same tolerance as an unknown class today; making a new one requires a
    new `vector/sources/*.py` module and a line here.
    """
    from itop_ai_assistant.vector.sources.faq import FAMILY as FAQ_FAMILY
    from itop_ai_assistant.vector.sources.faq import FaqVectorSource
    from itop_ai_assistant.vector.sources.tickets import FAMILY as TICKETS_FAMILY
    from itop_ai_assistant.vector.sources.tickets import TicketVectorSource

    # A source is given one repository, not the set: it has no business reaching
    # for another one. Two accessors, not one, and neither can answer as the
    # other identity: the sweep's can only ever be the service account's, the
    # confirmation's can only ever be the caller's (TASK-032). `for_principal`
    # itself stays here — a source cannot reach it, so `prepare()` has no way
    # to start indexing as somebody.
    async def ticket_repo() -> TicketRepository:
        return (await itop.for_principal(Principal.service(), comment=_SWEEP_COMMENT)).ticket_repo

    async def faq_repo() -> FaqRepository:
        return (await itop.for_principal(Principal.service(), comment=_SWEEP_COMMENT)).faq_repo

    async def ticket_repo_as(principal: Principal) -> TicketRepository:
        return (await itop.for_principal(principal, comment=_CONFIRM_COMMENT)).ticket_repo

    async def faq_repo_as(principal: Principal) -> FaqRepository:
        return (await itop.for_principal(principal, comment=_CONFIRM_COMMENT)).faq_repo

    builders: dict[str, Callable[[list[str]], "VectorSource[Any]"]] = {
        TICKETS_FAMILY: lambda classes: TicketVectorSource(ticket_repo, ticket_repo_as, classes=classes),
        FAQ_FAMILY: lambda classes: FaqVectorSource(faq_repo, faq_repo_as, classes=classes),
    }
    sources: list["VectorSource[Any]"] = []
    for name, builder in builders.items():
        family_cfg = cfg.families.get(name, FamilyConfig())
        sources.append(builder(list(family_cfg.classes)))
    for name in cfg.families:
        if name not in builders:
            logger.warning(f"vector: family {name!r} in config matches no registered source — ignoring")
    return sources
