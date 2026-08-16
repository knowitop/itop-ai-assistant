"""Public surface of the vector layer — the only way in from outside `vector/`.

Internals (`ports/`, `adapters/`, `use_cases/`, `state/`, `sources/`,
`router.py`, `chunker.py`) are free to reorganize; anything reached from
outside this package must be re-exported here first (`test_package_layers.py`,
`TestVectorFacade`). `sources/` is domain-specific (knows about `Ticket`,
iTop) but still internal — TASK-028 folded it in from the former top-level
`vector_sources/` precisely because nothing outside `vector/` needs more of
it than `TICKETS_FAMILY` below.

Two kinds of name live here (TASK-033): the contract-out a business module
actually calls (`SimilarSearch.available()`/`find()`, its exceptions,
`measure_embedding_dimension`) and the adapters the composition root still
assembles by hand (`QdrantChunkStore`, `register_vector_sweep`) because the
subsystem cannot yet build itself — that is stage 3's job, not this task's.
`EmbeddingsClient` is deliberately *not* re-exported any more: both of its
former external callers (`agents/intake/compose.py`, `admin/setup.py`) went
through the door instead, so the facade has nothing left to export it for.

`router.py` names `core.deps.AppDeps` in annotations only (`TYPE_CHECKING`,
never imported for real) — `core/deps.py` imports this facade's own adapters,
so a real import in either direction would deadlock: importing anything here
runs this file top to bottom, which imports `.router`, which would otherwise
import `core.deps` while it is still mid-import itself. `use_cases/indexer.py`
does not name it at all any more (TASK-029): the sweep takes its own
`IndexerDeps` port, which is what removed the last reason for the workaround
there rather than routing around it.
"""

from .adapters.qdrant_store import QdrantChunkStore
from .ports.query import FindStats, ObjectHit, SearchQuery, SearchResult
from .ports.store import ChunkStore, DateRange
from .router import router
from .sources.tickets import FAMILY as TICKETS_FAMILY
from .state.index_journal import IndexJournal
from .state.sync_state import VectorSyncState
from .use_cases.indexer import register_vector_sweep
from .use_cases.probe import measure_embedding_dimension
from .use_cases.search import SearchUnavailable, SimilarSearch, UnknownFamily

__all__ = [
    "ChunkStore",
    "DateRange",
    "FindStats",
    "IndexJournal",
    "ObjectHit",
    "QdrantChunkStore",
    "SearchQuery",
    "SearchResult",
    "SearchUnavailable",
    "SimilarSearch",
    "TICKETS_FAMILY",
    "UnknownFamily",
    "VectorSyncState",
    "measure_embedding_dimension",
    "register_vector_sweep",
    "router",
]
