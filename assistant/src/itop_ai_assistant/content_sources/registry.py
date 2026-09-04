"""Assembles the list of VectorSource instances the indexer sweeps.

Adding a new family: declare its `Schema` in `domain/`, its `ObjectType` in
`content_sources/<name>.py`, and add it to `OBJECT_TYPES` below. There is no
class to write and no builder to add — every family is swept by the same
`GenericVectorSource`.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from itop_ai_assistant.content_sources.faq import OBJECT_TYPE as FAQ
from itop_ai_assistant.content_sources.generic import GenericVectorSource, ObjectType
from itop_ai_assistant.content_sources.tickets import OBJECT_TYPE as TICKETS
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.schema import Role, Schema
from itop_ai_assistant.repositories.object_repo import ObjectRepository
from itop_ai_assistant.repositories.sets import ItopRepositories

if TYPE_CHECKING:
    # Named, not imported: `FamilyConfig` is imported for real inside
    # `build_vector_sources` below, where it is actually instantiated. Both
    # are annotations here and cost nothing at runtime.
    from itop_ai_assistant.vector import VectorConfig, VectorSource

logger = logging.getLogger(__name__)

#: Every family the indexer knows. The whole registry — what used to be a
#: builder per family is one entry now, because what differs between them is
#: entirely declared (`content_sources/generic.py::ObjectType`).
OBJECT_TYPES: tuple[ObjectType, ...] = (TICKETS, FAQ)

# Neither is a write today — sweep and confirm are both reads — but
# `for_principal` requires a comment, and this subsystem has no run to name.
_SWEEP_COMMENT = "AI assistant · vector · sweep"
_CONFIRM_COMMENT = "AI assistant · vector · confirming search candidates"


def declared_org_fields(schemas: Mapping[str, Schema]) -> dict[str, tuple[str, ...]]:
    """Per family, the semantic fields a source will accept in
    `VectorClassConfig.acl_org_fields`.

    Read off the family schemas rather than off built instances: the caller —
    `admin/setup.py`, validating a saved `vector` section — has no iTop
    repositories to build a source with and no business acquiring any. It
    passes the schemas *this deployment* has, so a field an administrator
    declared can grant access exactly like a built-in one.
    """
    return {obj.name: schemas.get(obj.name, obj.schema).names(Role.ORGANIZATION) for obj in OBJECT_TYPES}


def build_vector_sources(
    itop: ItopRepositories, cfg: "VectorConfig", schemas: Mapping[str, Schema]
) -> list["VectorSource[Any]"]:
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

    `schemas` is the families as this deployment has them — the code's
    declaration plus whatever the administrator added
    (`config.py::MappingsConfig.schemas`), so a declared field is composable
    into a fragment and can grant access like any other.

    A `cfg.families` key that matches no `ObjectType` is logged and skipped —
    the family name is not something the admin can invent from the UI, same
    tolerance as an unknown class today; making a new one requires a
    declaration and a line in `OBJECT_TYPES`.
    """
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

    known = {obj.name for obj in OBJECT_TYPES}
    for name in cfg.families:
        if name not in known:
            logger.warning(f"vector: family {name!r} in config matches no registered source — ignoring")
    return [
        GenericVectorSource(
            obj,
            sweeper(obj.name),
            confirmer(obj.name),
            family_cfg=cfg.families.get(obj.name, FamilyConfig()),
            schema=schemas.get(obj.name),
        )
        for obj in OBJECT_TYPES
    ]
