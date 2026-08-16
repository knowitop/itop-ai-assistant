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
  (Redis operational state), `sources/` (domain-specific `VectorSource`
  implementations — `Ticket`, iTop-aware, unlike everything else here),
  `chunker.py` (domain). Code outside `vector/` imports only
  `itop_ai_assistant.vector` (the facade in `__init__.py`), never a
  submodule — enforced by `test_package_layers.py::TestVectorFacade`. A new
  name a consumer needs goes into `vector/__init__.py`'s re-export list, not
  around it — including `core/deps.py`, which wires the concrete adapters
  through the facade like any other consumer (`router.py` names `AppDeps` in
  `TYPE_CHECKING` only, and `use_cases/indexer.py` no longer names it at
  all — it takes its own `IndexerDeps` port, TASK-029 — so there is no cycle
  to route around). What the facade re-exports is itself two tiers
  (TASK-033): the contract-out (`SimilarSearch`, its value types and
  exceptions, `measure_embedding_dimension`) any business module may import,
  and the concrete adapters (`QdrantChunkStore`, `register_vector_sweep`,
  `router`, …) that only the composition root and entry points may still
  reach — `test_package_layers.py::TestOnlyTheContractIsImportedFromOutside`
  checks the split; stage 3 of `alignment-plan.md` shrinks the adapter half
  further, not the test.
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
- The indexer knows nothing about iTop or tickets. It drives the `VectorSource`
  protocol; which iTop attributes a record maps to is the source's concern.
  Adding a source = a new `vector/sources/<name>.py` plus one line in
  `vector/sources/registry.py` — **no other file under `vector/` changes**.
  `sources/` living inside `vector/` (TASK-028) does not weaken this: the
  invariant was always about import direction, not package boundaries —
  `use_cases/indexer.py` still only ever reaches a concrete source through
  `build_vector_sources()`, never `sources/tickets.py`/`sources/faq.py`
  directly (`test_package_layers.py::TestVectorSourcesBoundary`).
- **The source declares its fragments (`fields`, `fragments`) and their
  `visibility`; the config only picks fields and toggles opt-in fragments**
  (ADR-018). `chunker.py` sees `TextContent`/`SequenceContent` and no domain
  names at all — it must never grow a rule keyed on a chunk kind. Text handed
  to it is expected canonical: `clean_text` is **not idempotent** (markdownify
  escapes markdown), so the source calls it exactly once.
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
  lifetime — a caller brings only `store`/`config`/`itop` at construction, and
  a `SearchQuery` plus a `Principal` per call** (TASK-033, rule 9.4). It is
  long-lived (`AppDeps.vector_search`, one per process) but re-reads
  `vector`/`embeddings` config on every `available()`/`find()`, so an admin
  edit applies without a restart; the `EmbeddingsClient` is created and
  `aclose()`d around one `find()`, never threaded through a run to be closed
  by someone else. `available()` is the availability gate a module checks
  before offering a tool; `find()` on an unavailable deployment raises
  `SearchUnavailable` instead of quietly returning nothing.
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
