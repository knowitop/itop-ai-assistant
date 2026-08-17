---
paths:
  - "assistant/src/itop_ai_assistant/content_sources/**"
---

# Content sources

Content providers (`tickets.py`, `faq.py`, `registry.py`) implement the
vector subsystem's `VectorSource` protocol (`vector/ports/source.py`) — see
`.claude/rules/vector.md` for that contract and `dev-docs/architecture/vector.md`
for the sweep mechanics they feed. This package moved out of `vector/`
entirely in TASK-035 (reversing TASK-028), reflecting the underlying rule:
the vector subsystem must not know the domain a source is written for (rule
6.4), and a source needs the domain (`Ticket`/`FaqArticle`, their
repositories) to do its job at all.

- **Four levels of authority, none overlapping** (ADR-018): the source
  decides which fragments exist, their `visibility`, and what field/log feeds
  each one; the config (`vector.classes[<class>].chunks`) only picks which
  semantic fields fill a required fragment and toggles an opt-in one on/off;
  `chunker.py` only knows how to pack already-resolved content by budget —
  no chunk-kind names, no domain; the consuming business module
  (`intake.similar_chunk_kinds`) decides which fragments to search. Getting
  this backwards — the chunker inferring `visibility` from a chunk-kind name,
  for instance — is exactly the bug ADR-018 fixed.
- **A source needs the vector facade's eleven contract names to implement
  `VectorSource` at all** — `Chunk`/`FragmentContent`/`FragmentSpec`/
  `SequenceContent`/`TextContent`/`VectorRecord`/`VectorSource`/
  `chunk_object`/`clean_text`/`ChunkFragmentConfig`/`VectorClassConfig` (the
  last two since TASK-036: `vector/config.py`'s per-class chunking config,
  not an adapter), imported from `itop_ai_assistant.vector` (never a deep
  `vector.chunker`/`vector.ports.source`/`vector.config` import —
  `test_package_layers.py::TestVectorFacade` catches that from any file
  outside `vector/`, `content_sources/` included since TASK-035).
  `registry.py` additionally takes `VectorConfig`/`FamilyConfig` from the
  same facade — the section as a whole, not just the per-class vocabulary —
  to build `build_vector_sources(itop, cfg)`.
- **Two identities, not one** (TASK-032). A source's `prepare()`/
  `find_modified_since()`/`find_existing_ids()` run as the service account —
  the sweep is not a run, the index it builds is global. `confirm_visible(principal,
  obj_class, ids)` runs as whoever is asking — the only honest answer to "may
  this person see it" (ADR-003). `content_sources/registry.py::build_vector_sources`
  hands each source two separate closures, one per identity, and neither
  reaches `ItopRepos.for_principal` itself — a source cannot start sweeping
  as somebody, and cannot answer a confirmation as the service account by
  accident. `test_package_layers.py::TestRightsCannotBeForgotten` pins that
  `confirm_visible`'s `principal` parameter has no default, in every
  registered source.
- **`registry.py`'s `ItopRepos` protocol is declared here, by the consumer**
  (`.claude/rules/core.md`: declare a port at the consumer rather than import
  one from wherever it happens to be defined) — not the same type as
  `pipelines.ports.ItopAccess`, which has the identical one method, because
  importing that one here would reach back into a package that itself imports
  the vector facade.
- Every registered family is built unconditionally by `build_vector_sources`,
  with `classes` read fresh from `cfg.families` on every call — not once at
  startup — so the admin UI's chunking vocabulary (`GET /api/vector/sources`)
  survives a family being emptied or removed from the saved config by
  mistake, and a family added or removed live applies without a restart
  (TASK-021).
- iTop access itself follows `.claude/rules/itop.md` like any other
  repository consumer — nothing here is exempt from "nothing outside the
  repositories touches iTop".
