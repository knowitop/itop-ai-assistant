"""Assembles the list of VectorSource instances the indexer sweeps.

Adding a new source: create `src/vector_sources/<name>.py` implementing
`vector.source.VectorSource`, and add one line to `_BUILDERS` below — same
pattern as `pipelines/registry.py` for webhook modules.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from itop_ai_assistant.config import FamilyConfig, VectorConfig
from itop_ai_assistant.faq_repository import FaqRepository
from itop_ai_assistant.ticket_repository import TicketRepository

if TYPE_CHECKING:
    from itop_ai_assistant.vector.source import VectorSource

logger = logging.getLogger(__name__)


class ItopRepos(Protocol):
    """The repository accessors a source is built from — declared here, by the
    consumer, rather than imported from the container that happens to offer them.

    Sources are built by the service account and index globally, so this port
    deliberately has no `for_principal`: whatever a given person may see is
    decided at search time, not at indexing time.
    """

    async def ticket_repo(self) -> TicketRepository: ...

    async def faq_repo(self) -> FaqRepository: ...


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
    new `vector_sources/*.py` module and a line here.
    """
    from itop_ai_assistant.vector_sources.faq import FAMILY as FAQ_FAMILY
    from itop_ai_assistant.vector_sources.faq import FaqVectorSource
    from itop_ai_assistant.vector_sources.tickets import FAMILY as TICKETS_FAMILY
    from itop_ai_assistant.vector_sources.tickets import TicketVectorSource

    builders: dict[str, Callable[[ItopRepos, list[str]], "VectorSource[Any]"]] = {
        TICKETS_FAMILY: lambda i, classes: TicketVectorSource(i.ticket_repo, classes=classes),
        FAQ_FAMILY: lambda i, classes: FaqVectorSource(i.faq_repo, classes=classes),
    }
    sources: list["VectorSource[Any]"] = []
    for name, builder in builders.items():
        family_cfg = cfg.families.get(name, FamilyConfig())
        sources.append(builder(itop, list(family_cfg.classes)))
    for name in cfg.families:
        if name not in builders:
            logger.warning(f"vector: family {name!r} in config matches no registered source — ignoring")
    return sources
