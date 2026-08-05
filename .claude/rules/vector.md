---
paths:
  - "assistant/src/itop_ai_assistant/vector/**"
  - "assistant/src/itop_ai_assistant/vector_sources/**"
  - "assistant/test/pg/**"
---

# Vector layer

Mechanics (sweep, cursors, the renewed lock, reconciliation, fingerprints):
`dev-docs/architecture/vector.md`. The backend follows
`dev-docs/decisions/ADR-001-vector-store-qdrant.md` — Qdrant, not pgvector.

- **Infrastructure, not a business module.** It does not register in
  `TriggerRegistry`, has no prompts and no trigger routes. Business modules
  consume it through `AppDeps.vector_store`.
- **Never store raw object text in the chunk collections** — embeddings, ids
  and filter metadata only. This matters more on Qdrant than it did on a
  relational store: payload takes arbitrary JSON with no schema to catch a
  stray text field.
- The indexer knows nothing about iTop or tickets. It drives the `VectorSource`
  protocol; which iTop attributes a record maps to is the source's concern.
  Adding a source = a new `vector_sources/<name>.py` plus one line in
  `vector_sources/registry.py`, and **no change under `vector/`**.
- The whole subsystem is off when `qdrant_url` is unset — every code path must
  survive a Redis-only deployment.
- Sweep cursors, the reindex flag and the run journal live in Redis
  (`vector/sync_state.py`, `vector/index_journal.py`), never in the
  `ChunkStore` — the port stays a pure vector store and grows no operational
  state (`test_port_does_not_leak_sync_state`). Cross-replica exclusion is a
  Redis lock renewed for the length of the pass, not the original TTL — see
  `dev-docs/architecture/vector.md`.
- The index is built by the service account and is global. Search returns
  **candidates**; what a given person may see is decided by resolving hits under
  their own token.
- The whole vector unit test suite (`test/unit/test_qdrant_*.py` and friends)
  is collected by default — it runs against Qdrant's `:memory:` mode, no
  Docker needed.
- **A payload field that filtering depends on must feed `ChunkMetadata.meta_hash`**
  (`vector/store.py`). The sweep refreshes such a field without a re-embed
  only through that hash (TASK-003); leave a new one out and it freezes at
  whatever value the chunk had when it was first embedded, silently, until
  the text changes for some unrelated reason.
