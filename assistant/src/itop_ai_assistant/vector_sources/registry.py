"""Assembles the list of VectorSource instances the indexer sweeps.

Adding a new source: create `src/vector_sources/<name>.py` implementing
`vector.source.VectorSource`, and add one line below — same pattern as
`pipelines/registry.py` for webhook modules.
"""

from typing import TYPE_CHECKING

from itop_ai_assistant.config import VectorConfig

if TYPE_CHECKING:
    from itop_ai_assistant.deps import AppDeps
    from itop_ai_assistant.vector.source import VectorSource


def build_vector_sources(deps: "AppDeps", cfg: VectorConfig) -> list["VectorSource"]:
    from itop_ai_assistant.vector_sources.faq import FaqVectorSource
    from itop_ai_assistant.vector_sources.tickets import TicketVectorSource

    # `cfg.classes` mixes classes from every source; each source is built
    # only with the ones it actually owns (TASK-016 — the first time a
    # second source exists, so this split used to be unconditional).
    ticket_classes = [c for c in cfg.classes if c != "FAQ"]
    sources: list["VectorSource"] = []
    if ticket_classes:
        sources.append(TicketVectorSource(deps, classes=ticket_classes))
    if "FAQ" in cfg.classes:
        sources.append(FaqVectorSource(deps, classes=["FAQ"]))
    return sources
