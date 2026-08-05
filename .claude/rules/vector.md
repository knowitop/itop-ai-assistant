---
paths:
  - "assistant/src/itop_ai_assistant/vector/**"
  - "assistant/src/itop_ai_assistant/vector_sources/**"
  - "assistant/test/pg/**"
---

# Vector layer

Mechanics (sweep, cursors, advisory lock, reconciliation, fingerprints):
`dev-docs/architecture/vector.md`.

- **Infrastructure, not a business module.** It does not register in
  `TriggerRegistry`, has no prompts and no trigger routes. Business modules
  consume it through `AppDeps.vector_db`.
- **Never store raw object text in the chunk tables** — embeddings, ids and
  filter metadata only.
- The indexer knows nothing about iTop or tickets. It drives the `VectorSource`
  protocol; which iTop attributes a record maps to is the source's concern.
  Adding a source = a new `vector_sources/<name>.py` plus one line in
  `vector_sources/registry.py`, and **no change under `vector/`**.
- The whole subsystem is off when `database_url` is unset — every code path must
  survive a Redis-only deployment. Migration failures degrade to a warning, never
  a boot failure.
- The index is built by the service account and is global. Search returns
  **candidates**; what a given person may see is decided by resolving hits under
  their own token.
- Postgres tests (`test/pg/`) are not collected by default: `uv run pytest test/pg`.
