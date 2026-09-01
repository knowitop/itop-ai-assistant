"""Public surface of the vector layer — the only way in from outside `vector/`.

Internals (`ports/`, `adapters/`, `use_cases/`, `state/`, `router.py`,
`assembly.py`, `chunker.py`) are free to reorganize; anything reached from
outside this package must be re-exported here first (`test_package_layers.py`,
`TestVectorFacade`). Content providers (`content_sources/` — `Ticket`,
the family schemas, iTop) are a consumer of this facade like any other business
module, not part of it — see `.claude/rules/content-sources.md`.

One name assembles the whole subsystem for the composition root —
`build(settings, redis, config_store, itop) -> VectorSubsystem`
(`assembly.py`, TASK-037) — replacing what used to be five adapters
(`QdrantChunkStore`, `IndexJournal`, `VectorSyncState`, a `vector_sources`
closure, and `SimilarSearch`) that `core/deps.py` assembled by hand into five
separate `AppDeps` fields. `core/deps.py` keeps the result in one field
(`AppDeps.vector`); `vector/router.py` reaches the same instance through
`request.app.state.vector`, not through `AppDeps` at all — which is what
retired the `TYPE_CHECKING` cycle workaround this docstring used to explain:
`core/deps.py` no longer imports any of this package's concrete adapters, so
there is nothing left for `router.py` or `core/api_deps.py` to route around.

Everything else here is the contract-out a business module actually calls:
`SimilarSearch.available()`/`find()`, its exceptions, `measure_embedding_dimension`,
plus the vocabulary `content_sources/` needs to implement `VectorSource` at
all (`Chunk`, `ChunkPlan`, `FragmentContent`, `FragmentSpec`, `SequenceContent`,
`TextContent`, `VectorRecord`, `VectorSource`, `chunk_object`, `clean_text`,
TASK-040), and `VectorConfig`/`FamilyConfig` for the same reason:
`content_sources/registry.py`'s `build_vector_sources` and `admin/setup.py`
both need the section's shape.
`EmbeddingsClient` and `build_vector_sources` are deliberately *not*
re-exported — neither is this package's business to shield; every caller of
`EmbeddingsClient` goes through the `SimilarSearch`/`measure_embedding_dimension`
door instead, and `build_vector_sources` has exactly one caller left,
`assembly.py`, which imports it directly from `content_sources.registry`.
"""

from .assembly import VectorSubsystem, build
from .chunker import Chunk, FragmentContent, SequenceContent, TextContent, chunk_object, clean_text
from .config import FamilyConfig, VectorConfig
from .domain import DateRange
from .ports.query import FindStats, ObjectHit, SearchQuery, SearchResult
from .ports.source import ChunkPlan, FragmentSpec, VectorRecord, VectorSource
from .router import router
from .use_cases.indexer import register_vector_sweep
from .use_cases.probe import measure_embedding_dimension
from .use_cases.search import SearchUnavailable, SimilarSearch, UnknownFamily

__all__ = [
    "Chunk",
    "ChunkPlan",
    "DateRange",
    "FamilyConfig",
    "FindStats",
    "FragmentContent",
    "FragmentSpec",
    "ObjectHit",
    "SearchQuery",
    "SearchResult",
    "SearchUnavailable",
    "SequenceContent",
    "SimilarSearch",
    "TextContent",
    "UnknownFamily",
    "VectorConfig",
    "VectorRecord",
    "VectorSource",
    "VectorSubsystem",
    "build",
    "chunk_object",
    "clean_text",
    "measure_embedding_dimension",
    "register_vector_sweep",
    "router",
]
