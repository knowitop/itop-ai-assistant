"""Public surface of the vector layer — the only way in from outside `vector/`.

Internals (`ports/`, `adapters/`, `use_cases/`, `state/`, `router.py`,
`chunker.py`) are free to reorganize; anything reached from outside this
package must be re-exported here first (`test_package_layers.py`,
`TestVectorFacade`). Content providers (`content_sources/` — `Ticket`,
`FaqArticle`, iTop) are a consumer of this facade like any other business
module, not part of it — see `.claude/rules/content-sources.md`.

Two kinds of name live here: the contract-out a business module actually
calls (`SimilarSearch.available()`/`find()`, its exceptions,
`measure_embedding_dimension`, plus the vocabulary `content_sources/` needs
to implement `VectorSource` at all: `Chunk`, `FragmentContent`,
`FragmentSpec`, `SequenceContent`, `TextContent`, `VectorRecord`,
`VectorSource`, `chunk_object`, `clean_text`, `ChunkFragmentConfig`,
`VectorClassConfig` — the last two are `content_sources/`'s own chunking
config, not an adapter, TASK-036) and the adapters the composition root still
assembles by hand (`QdrantChunkStore`, `register_vector_sweep`) because the
subsystem cannot yet build itself. `VectorConfig`/`FamilyConfig` (TASK-036,
`vector/config.py`) are contract-out too, for the same reason as the pair
above: `content_sources/registry.py::build_vector_sources` and
`admin/setup.py` both need the section's shape, not an adapter built from it.
`EmbeddingsClient` and `build_vector_sources` are deliberately *not*
re-exported: neither is this package's business to shield — every caller of
`EmbeddingsClient` goes through the `SimilarSearch`/`measure_embedding_dimension`
door instead, and `core/deps.py` imports `build_vector_sources` directly from
`content_sources.registry`.

`router.py` names `core.deps.AppDeps` in annotations only (`TYPE_CHECKING`,
never imported for real) — `core/deps.py` imports this facade's own adapters,
so a real import in either direction would deadlock: importing anything here
runs this file top to bottom, which imports `.router`, which would otherwise
import `core.deps` while it is still mid-import itself. `use_cases/indexer.py`
does not name `AppDeps` at all: the sweep takes its own `IndexerDeps` port
instead. The same class of cycle would recur if `router.py` ever imported
`content_sources.*` at module level for a name that package's own top-level
code needs back from this facade (`Chunk` and friends) — it currently does
not.
"""

from .adapters.qdrant_store import QdrantChunkStore
from .chunker import Chunk, FragmentContent, SequenceContent, TextContent, chunk_object, clean_text
from .config import ChunkFragmentConfig, FamilyConfig, VectorClassConfig, VectorConfig
from .ports.query import FindStats, ObjectHit, SearchQuery, SearchResult
from .ports.source import FragmentSpec, VectorRecord, VectorSource
from .ports.store import ChunkStore, DateRange
from .router import router
from .state.index_journal import IndexJournal
from .state.sync_state import VectorSyncState
from .use_cases.indexer import register_vector_sweep
from .use_cases.probe import measure_embedding_dimension
from .use_cases.search import SearchUnavailable, SimilarSearch, UnknownFamily

__all__ = [
    "Chunk",
    "ChunkFragmentConfig",
    "ChunkStore",
    "DateRange",
    "FamilyConfig",
    "FindStats",
    "FragmentContent",
    "FragmentSpec",
    "IndexJournal",
    "ObjectHit",
    "QdrantChunkStore",
    "SearchQuery",
    "SearchResult",
    "SearchUnavailable",
    "SequenceContent",
    "SimilarSearch",
    "TextContent",
    "UnknownFamily",
    "VectorClassConfig",
    "VectorConfig",
    "VectorRecord",
    "VectorSource",
    "VectorSyncState",
    "chunk_object",
    "clean_text",
    "measure_embedding_dimension",
    "register_vector_sweep",
    "router",
]
