"""Assembles the list of VectorSource instances the indexer sweeps.

Adding a new source: create `src/vector_sources/<name>.py` implementing
`vector.source.VectorSource`, and add one line below — same pattern as
`pipelines/registry.py` for webhook modules.
"""

from typing import TYPE_CHECKING

from config import VectorConfig

if TYPE_CHECKING:
    from deps import AppDeps
    from vector.source import VectorSource


def build_vector_sources(deps: "AppDeps", cfg: VectorConfig) -> list["VectorSource"]:
    from vector_sources.tickets import TicketVectorSource

    return [TicketVectorSource(deps, classes=list(cfg.classes))]
