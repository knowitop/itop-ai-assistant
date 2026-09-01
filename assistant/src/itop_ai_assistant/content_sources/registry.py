"""Assembles the list of VectorSource instances the indexer sweeps.

Adding a new source: create `content_sources/<name>.py` implementing
`vector.ports.source.VectorSource`, and add one line to `_BUILDERS` below —
same pattern as `pipelines/registry.py` for webhook modules.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.schema import Role
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA
from itop_ai_assistant.repositories.object_repo import ObjectRepository
from itop_ai_assistant.repositories.sets import ItopRepositories

if TYPE_CHECKING:
    # `vector/assembly.py` imports this module for real (`build_vector_sources`),
    # and `vector/__init__.py` imports `.assembly` — a real import of the
    # facade here, at module level, would deadlock on that cycle the same way
    # a real `core.deps` import would (see `vector/__init__.py`'s own
    # docstring). `FamilyConfig` is imported again, for real, inside
    # `build_vector_sources` below, where it is actually instantiated — by
    # then every module involved has finished importing, so the same cycle
    # cannot occur.
    from itop_ai_assistant.vector import VectorConfig, VectorSource

logger = logging.getLogger(__name__)

# Neither is a write today — sweep and confirm are both reads — but
# `for_principal` requires a comment, and this subsystem has no run to name.
_SWEEP_COMMENT = "AI assistant · vector · sweep"
_CONFIRM_COMMENT = "AI assistant · vector · confirming search candidates"


def declared_org_fields() -> dict[str, tuple[str, ...]]:
    """Per family, the semantic fields a source will accept in
    `VectorClassConfig.acl_org_fields`.

    Read off the family schemas rather than off built instances: the
    declaration is static (as `GET /api/vector/sources` already says of the
    others), and the caller — `admin/setup.py`, validating a saved `vector`
    section — has no iTop repositories to build a source with and no business
    acquiring any. A source class is not needed to answer this at all: the
    fields that can grant access are a property of the family.
    """
    return {schema.name: schema.names(Role.ORGANIZATION) for schema in (TICKET_SCHEMA, FAQ_SCHEMA)}


def build_vector_sources(itop: ItopRepositories, cfg: "VectorConfig") -> list["VectorSource[Any]"]:
    """One instance per *registered* family, not per family the saved config
    happens to still mention.

    Every known family is built unconditionally, from
    `cfg.families.get(name, FamilyConfig())` — an empty family config if the
    admin cleared it or the key is missing entirely. The whole section, not
    just its class list: a source reads its per-class settings (which fields
    grant access, `VectorClassConfig.acl_org_fields`) out of it too. That is what lets the admin UI
    (`GET /api/vector/sources`) always show a family's full chunking
    vocabulary: a family absent from the saved config still gets a vocabulary
    to offer, so an admin can recover a class removed by mistake instead of
    the family disappearing from the editor entirely.

    A `cfg.families` key that matches no builder below is logged and
    skipped — the family name is not something the admin can invent from the
    UI, same tolerance as an unknown class today; making a new one requires a
    new `content_sources/*.py` module and a line here.
    """
    from itop_ai_assistant.content_sources.faq import FAMILY as FAQ_FAMILY
    from itop_ai_assistant.content_sources.faq import FaqVectorSource
    from itop_ai_assistant.content_sources.tickets import FAMILY as TICKETS_FAMILY
    from itop_ai_assistant.content_sources.tickets import TicketVectorSource
    from itop_ai_assistant.vector import FamilyConfig

    # A source is given one repository, not the set: it has no business reaching
    # for another one. Two accessors, not one, and neither can answer as the
    # other identity: the sweep's can only ever be the service account's, the
    # confirmation's can only ever be the caller's. `for_principal`
    # itself stays here — a source cannot reach it, so `prepare()` has no way
    # to start indexing as somebody.
    def sweeper(family: str) -> Callable[[], Awaitable[ObjectRepository]]:
        async def repo() -> ObjectRepository:
            return (await itop.for_principal(Principal.service(), comment=_SWEEP_COMMENT)).objects[family]

        return repo

    def confirmer(family: str) -> Callable[[Principal], Awaitable[ObjectRepository]]:
        async def repo_as(principal: Principal) -> ObjectRepository:
            return (await itop.for_principal(principal, comment=_CONFIRM_COMMENT)).objects[family]

        return repo_as

    builders: dict[str, Callable[["FamilyConfig"], "VectorSource[Any]"]] = {
        TICKETS_FAMILY: lambda family_cfg: TicketVectorSource(
            sweeper(TICKETS_FAMILY), confirmer(TICKETS_FAMILY), family_cfg=family_cfg
        ),
        FAQ_FAMILY: lambda family_cfg: FaqVectorSource(
            sweeper(FAQ_FAMILY), confirmer(FAQ_FAMILY), family_cfg=family_cfg
        ),
    }
    sources: list["VectorSource[Any]"] = []
    for name, builder in builders.items():
        sources.append(builder(cfg.families.get(name, FamilyConfig())))
    for name in cfg.families:
        if name not in builders:
            logger.warning(f"vector: family {name!r} in config matches no registered source — ignoring")
    return sources
