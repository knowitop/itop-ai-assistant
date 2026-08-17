---
paths:
  - "assistant/src/itop_ai_assistant/vector/**"
  - "assistant/test/pg/**"
---

# Vector layer

Mechanics (sweep, cursors, the renewed lock, reconciliation, fingerprints):
`dev-docs/architecture/vector.md`. The backend follows
`dev-docs/decisions/ADR-001-vector-store-qdrant.md` — Qdrant, not pgvector.

- **Layered internally, entered only through the facade** (TASK-026, TASK-028):
  `router.py` (transport), `ports/` (protocols + value objects), `adapters/`
  (`QdrantChunkStore`, `EmbeddingsClient`), `use_cases/` (`VectorIndexer`,
  `SimilarSearch`, `measure_embedding_dimension`, reindex CLI), `state/`
  (Redis operational state), `chunker.py` (domain-agnostic packing). Content
  providers (`Ticket`, `FaqArticle`, iTop-aware) are **not** part of this
  list — `content_sources/` is a sibling package now, not a subpackage
  (TASK-035, reversing TASK-028's earlier call to fold it in — see
  `.claude/rules/content-sources.md`). Code outside `vector/` imports only
  `itop_ai_assistant.vector` (the facade in `__init__.py`), never a
  submodule — enforced by `test_package_layers.py::TestVectorFacade`, and
  since TASK-035 that includes `content_sources/` itself. A new name a
  consumer needs goes into `vector/__init__.py`'s re-export list, not
  around it — including `core/deps.py`, which wires the concrete adapters
  through the facade like any other consumer (`router.py` names `AppDeps` in
  `TYPE_CHECKING` only, and `use_cases/indexer.py` no longer names it at
  all — it takes its own `IndexerDeps` port, TASK-029 — so there is no cycle
  to route around). What the facade re-exports is itself two tiers
  (TASK-033): the contract-out (`SimilarSearch`, its value types and
  exceptions, `measure_embedding_dimension`, and — since TASK-035 — the nine
  names `content_sources/` needs to implement `VectorSource` at all:
  `Chunk`/`FragmentContent`/`FragmentSpec`/`SequenceContent`/`TextContent`/
  `VectorRecord`/`VectorSource`/`chunk_object`/`clean_text`) any business
  module may import, and the concrete adapters (`QdrantChunkStore`,
  `register_vector_sweep`, `router`, …) that only the composition root and
  entry points may still reach —
  `test_package_layers.py::TestOnlyTheContractIsImportedFromOutside` checks
  the split; stage 3 of `alignment-plan.md` shrinks the adapter half
  further, not the test. `build_vector_sources` left the facade entirely in
  TASK-035 — it lives in `content_sources.registry` now, and `core/deps.py`
  imports it from there directly, since there is nothing left in `vector/`
  for the facade to shield.
- **Infrastructure, not a business module.** It does not register in
  `TriggerRegistry`, has no prompts and no trigger routes. Business modules
  consume it through `RunDeps.vector_search`/`AppDeps.vector_search` — one
  door (`SimilarSearch.available()`/`find(query, principal)`), not a
  `ChunkStore` a module would have to assemble a search from itself
  (TASK-033).
- **Never store raw object text in the chunk collections** — embeddings, ids
  and filter metadata only. This matters more on Qdrant than it did on a
  relational store: payload takes arbitrary JSON with no schema to catch a
  stray text field.
- **Nothing under `vector/` knows the ticket or FAQ domain — not just the
  indexer, all of it** (rule 6.4, TASK-035 strengthens what TASK-028's narrower
  `TestVectorSourcesBoundary` checked). Adding a source = a new
  `content_sources/<name>.py` plus one line in `content_sources/registry.py`
  — **no file under `vector/` changes**, checked directly now:
  `test_package_layers.py::TestVectorDoesNotKnowContentDomains` bans
  `domain.ticket`/`domain.faq`/`repositories.ticket`/`repositories.faq`
  anywhere in the `vector/` tree, not just in `use_cases/indexer.py`.
  `content_sources/` itself is where the source declares its fragments
  (`fields`, `fragments`) and their `visibility`, and where the two-identity
  confirmation (TASK-032) is implemented — see `.claude/rules/content-sources.md`
  for both; `chunker.py` only sees `TextContent`/`SequenceContent` and no
  domain names at all, and must never grow a rule keyed on a chunk kind. Text
  handed to it is expected canonical: `clean_text` is **not idempotent**
  (markdownify escapes markdown), so the source calls it exactly once.
- The whole subsystem is off when `qdrant_url` is unset — every code path must
  survive a Redis-only deployment.
- Sweep cursors, the reindex flag and the run journal live in Redis
  (`vector/state/sync_state.py`, `vector/state/index_journal.py`), never in
  the `ChunkStore` — the port stays a pure vector store and grows no
  operational state (`test_port_does_not_leak_sync_state`). Cross-replica
  exclusion is a Redis lock renewed for the length of the pass, not the
  original TTL — see `dev-docs/architecture/vector.md`.
- The index is built by the service account and is global. Search returns
  **candidates**; what a given person may see is decided by confirming hits
  under their own token. **That confirmation is part of contract-in, not a
  callback** (TASK-032): a source declares two operations with two identities —
  `find_existing_ids` (the sweep's probe, service account) and
  `confirm_visible(principal, ...)` (the search's gate) — and
  `SimilarSearch.find(query, principal)` takes the principal, never a rights
  check. There is no way to call the search that skips the confirmation, and
  no way to confirm without naming who is asking
  (`test_package_layers.py::TestRightsCannotBeForgotten`). The subsystem names
  the principal with the platform's own `Principal` type — ADR-021 for why
  that is not the same as knowing a consumer's domain.
- **`SimilarSearch` owns its own configuration and the embeddings client's
  lifetime — a caller brings only `store`/`config`/`build_sources` at
  construction, and a `SearchQuery` plus a `Principal` per call** (TASK-033,
  rule 9.4). It is long-lived (`AppDeps.vector_search`, one per process) but
  re-reads `vector`/`embeddings` config on every `available()`/`find()`, so an
  admin edit applies without a restart; the `EmbeddingsClient` is created and
  `aclose()`d around one `find()`, never threaded through a run to be closed
  by someone else. `available()` is the availability gate a module checks
  before offering a tool; `find()` on an unavailable deployment raises
  `SearchUnavailable` instead of quietly returning nothing.
- **Neither `SimilarSearch` nor `VectorIndexer` imports
  `content_sources.registry` any more (TASK-034)** — `core/deps.py` is the
  only caller of `build_vector_sources()` left in the process, imported
  directly from `content_sources.registry` since TASK-035 (not through the
  vector facade — `build_vector_sources` is not this package's business any
  more). Both take a `VectorConfig -> Sequence[...]` builder instead:
  `SimilarSearch`'s constructor parameter `build_sources`, `VectorIndexer`'s
  via `IndexerDeps.vector_sources(cfg)`.
  The builder is called fresh on every `find()`/`sweep_once()`, never
  memoized, which is what keeps a family added or removed from the saved
  config live without a restart (TASK-021) — a list collected once at start
  would have broken that guarantee silently. `vector/router.py`'s `/status`
  and `/sources` get the same list through a `Depends` provider that reads
  `AppDeps.vector_sources`, the same way it already reaches `get_itop`/
  `get_vector_store` (`test_package_layers.py::TestSourcesAreInjectedNotBuilt`).
- **The R4 org pre-filter is the caller's, deliberately.** Layer 1
  (`AccessRepository.allowed_org_ids()` → `filters["org_id"]`) shapes the walk
  before it starts, is over-permissive by design (ADR-003) and means knowing
  what an organization is; only `vector/router.py`'s debug `/search` builds it
  today. Forgetting it costs recall, not confidentiality — that guarantee is
  layer 2's, and layer 2 is the one inside the contract.
- The whole vector unit test suite (`test/unit/test_qdrant_*.py` and friends)
  is collected by default — it runs against Qdrant's `:memory:` mode, no
  Docker needed.
- **A payload field that filtering depends on must feed `ChunkMetadata.meta_hash`**
  (`vector/ports/store.py`). The sweep refreshes such a field without a
  re-embed only through that hash (TASK-003); leave a new one out and it
  freezes at whatever value the chunk had when it was first embedded,
  silently, until the text changes for some unrelated reason. The rule has no
  exceptions — `created_at` was one until TASK-020 and no longer is.
- **What describes the object is computed once per record, not per chunk**
  (`_ObjectMetadata`, `vector/use_cases/indexer.py`). Rewrites are per-chunk, so an
  object-level value recomputed per chunk drifts apart between the chunks of
  one object — that is exactly how `created_at` broke. A source with no
  creation date inherits the one already in the index (`ChunkDigest.created_at`);
  the fallback to the sweep's clock fires once, at first indexing, and never
  again.
- `QdrantChunkStore._meta_cache` is a cache, not the operational state the
  rule above bans: every store call needs the active version to build a
  collection name, and the sweep calls the store once per object. It dies
  with the process and is rebuilt from `chunks_meta`; `ensure_version` is the
  only writer and the only thing that invalidates it.
