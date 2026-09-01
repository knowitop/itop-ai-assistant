"""The subsystem's own composition root — the one operation that assembles
`vector/` from the outside (rule 6.1, `ADR-019`, `alignment-plan.md` stage 3).

Before this module existed, `core/deps.py` imported `QdrantChunkStore`,
`IndexJournal`, `VectorSyncState` and `content_sources.registry.build_vector_sources`
directly and assembled them by hand into five separate `AppDeps` fields — the
composition root knew the subsystem's internals, which is what forced the
`TYPE_CHECKING` cycle workarounds documented (in the past tense now) by
`vector/__init__.py` and `vector/router.py`. `build()` is the replacement: it
takes exactly what the subsystem cannot get for itself and hands back one
opaque `VectorSubsystem`.

`settings`, `redis` and `config_store` are three different things, not one,
because each is genuinely something only the caller can supply:

- `settings.qdrant_url` is a bootstrap value — env-only, like `redis_url`
  (`config.py`) — so it comes from `Settings`, not the runtime-editable
  `vector` section.
- `redis` is the *same* connection pool `core/deps.py` already opened for
  `state_manager`/`config_store`/`prompt_store`/`journal` — building a second
  one here would open a second pool for no reason, purely because this
  function happens to be a different file now.
- `config_store` is likewise the one `core/deps.py` already built, not a
  second `RedisConfigStore(redis, settings)` constructed from the same two
  ingredients — a duplicate object with no duplicate data, and an import of a
  concrete class this subsystem otherwise has no reason to know about.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from itop_ai_assistant.config import Settings
from itop_ai_assistant.repositories.sets import ItopRepositories
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.state.counters import DailyCounters
from itop_ai_assistant.vector.adapters.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.config import VectorConfig
from itop_ai_assistant.vector.ports.source import VectorSource
from itop_ai_assistant.vector.ports.store import ChunkStore
from itop_ai_assistant.vector.state.index_journal import IndexJournal
from itop_ai_assistant.vector.state.sync_state import VectorSyncState
from itop_ai_assistant.vector.use_cases.search import SimilarSearch


@dataclass
class VectorSubsystem:
    """What the rest of the process may hold onto — `AppDeps.vector`, one
    field, and `vector/router.py`'s own `request.app.state.vector`.

    Field names match the parameters of `VectorIndexer.__init__`/
    `register_vector_sweep` (`vector/use_cases/indexer.py`) one for one on
    purpose (TASK-039): `core/background.py`/`reindex.py` unpack `deps.vector`
    into those five arguments without renaming anything.
    `itop` and `config_store` are kept, not just consumed and dropped, because
    `vector/router.py` needs both back (`/search`'s R4 pre-filter, and every
    endpoint's own read of the `vector`/`embeddings` sections) and it no
    longer has any other way to reach them.
    """

    config_store: ConfigStore
    itop: ItopRepositories
    vector_store: ChunkStore
    vector_sync: VectorSyncState
    vector_journal: IndexJournal
    vector_sources: Callable[[VectorConfig], Sequence[VectorSource[Any]]]
    vector_search: SimilarSearch

    async def aclose(self) -> None:
        """Closes the store's client. `AppDeps.aclose()` calls this
        transitively — ownership of the client moved here, the obligation to
        close it did not move anywhere."""
        await self.vector_store.aclose()


def build(
    settings: Settings,
    redis: Redis,
    config_store: ConfigStore,
    itop: ItopRepositories,
    counters: DailyCounters,
) -> VectorSubsystem:
    """Assemble the subsystem once, at startup — the composition root's one
    call into `vector/`, mirroring `build_registry(settings)` for modules.
    """
    # Imported here rather than at module level: `content_sources/` declares
    # its families against this package's facade, and the facade imports this
    # module — at import time that is a cycle, by the first call it is not.
    # The deferral lives here, at the one point where the dependency actually
    # runs backwards, instead of in every module `content_sources/` has.
    from itop_ai_assistant.content_sources.registry import build_vector_sources

    vector_store = QdrantChunkStore(settings.qdrant_url)

    # Re-read `cfg.families` fresh on every call, not collected once here: a
    # static list would break the live config reload (TASK-021).
    def vector_sources(cfg: VectorConfig) -> list[VectorSource[Any]]:
        return build_vector_sources(itop, cfg)

    return VectorSubsystem(
        config_store=config_store,
        itop=itop,
        vector_store=vector_store,
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
        vector_sources=vector_sources,
        vector_search=SimilarSearch(vector_store, config_store, vector_sources, counters),
    )
