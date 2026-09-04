---
paths:
  - "assistant/src/itop_ai_assistant/content_sources/**"
---

# Content sources

`generic.py` implements the vector subsystem's `VectorSource` protocol
(`vector/ports/source.py`) **once**, for every family — see
`.claude/rules/vector.md` for that contract and `dev-docs/architecture/vector.md`
for the sweep mechanics it feeds. This package moved out of `vector/`
entirely in TASK-035 (reversing TASK-028), reflecting the underlying rule:
the vector subsystem must not know the domain a source is written for (rule
6.4), and a source needs the domain (the family schemas and the object
repository) to do its job at all.

- **A family is a declaration, not a class** (ADR-034, TASK-077).
  `tickets.py`/`faq.py` are one `ObjectType` each — schema, fragments, the
  pre-filter fields, the payload keys to index — and `GenericVectorSource`
  does the rest for all of them. Adding a family: a `Schema` in `domain/`, an
  `ObjectType`, one entry in `registry.py::OBJECT_TYPES`. The chunking
  vocabulary is derived (`Role.CONTENT`), never listed; the relevance value
  and the modification date are roles, not names this package knows.
- **The payload is an `ObjectView`, not a typed model.** A source reads by
  semantic name or by role and needs no `Ticket` — `is_requester` is already
  marked on each case-log entry by the repository, which is what lets the
  conversation be labelled with nothing here knowing what a ticket is.
- **A source is built over the deployment's schema, not the code's.**
  `build_vector_sources(itop, cfg, schemas)` takes them, and
  `vector/assembly.py` reads the mapping section to produce them — so a field
  an administrator declared is in the chunking vocabulary, may be named in
  `acl_org_fields`, and rides into the payload. Only `id` and `enum` values
  do: prose in the payload is the one thing the index must never store.
- **The deferred import lives in `vector/assembly.py`, not here.**
  `content_sources/` declares against the vector facade, and the facade's
  `assembly` needs `build_vector_sources` — the one point where the
  dependency runs backwards is where the import is deferred.
- **Four levels of authority, none overlapping** (ADR-018): the source
  decides which fragments exist, their `visibility`, and what field/log feeds
  each one; the config (`vector.classes[<class>].chunks`) only picks which
  semantic fields fill a required fragment and toggles an opt-in one on/off;
  `chunker.py` only knows how to pack already-resolved content by budget —
  no chunk-kind names, no domain; the consuming business module
  (`intake.similar_chunk_kinds`) decides which fragments to search. Getting
  this backwards — the chunker inferring `visibility` from a chunk-kind name,
  for instance — is exactly the bug ADR-018 fixed.
- **A source needs the vector facade's ten contract names to implement
  `VectorSource` at all** — `Chunk`/`ChunkPlan`/`FragmentContent`/
  `FragmentSpec`/`SequenceContent`/`TextContent`/`VectorRecord`/
  `VectorSource`/`chunk_object`/`clean_text`, imported from
  `itop_ai_assistant.vector` (never a deep `vector.chunker`/
  `vector.ports.source` import — `test_package_layers.py::TestVectorFacade`
  catches that from any file outside `vector/`, `content_sources/` included
  since TASK-035). `chunk()`'s configuration parameter is `ChunkPlan`
  (`vector/ports/source.py`), not the settings model `VectorClassConfig` —
  the source never sees that pydantic type at all since TASK-040, only its
  two fields (`fields`, `enabled`) already resolved by the caller
  (`vector/use_cases/indexer.py::_chunk_plan`). `FragmentSpec` is what
  `Fragment` (the family's own declaration, which also names the case log an
  opt-in fragment reads) projects to. `registry.py` additionally
  takes `VectorConfig`/`FamilyConfig` from the same facade — the section as
  a whole, not just the per-class vocabulary — to build
  `build_vector_sources(itop, cfg)`.
- **Two identities, not one** (TASK-032). The source's `prepare()`/
  `find_modified_since()`/`find_existing_ids()` run as the service account —
  the sweep is not a run, the index it builds is global. `confirm_visible(principal,
  obj_class, ids)` runs as whoever is asking — the only honest answer to "may
  this person see it" (ADR-003). `content_sources/registry.py::build_vector_sources`
  hands each source two separate closures over `RepositorySet.objects[family]`,
  one per identity, and neither reaches `ItopRepositories.for_principal`
  itself — a source cannot start sweeping as somebody, and cannot answer a
  confirmation as the service account by accident.
  `test_package_layers.py::TestRightsCannotBeForgotten` pins that
  `confirm_visible`'s `principal` parameter has no default, in every
  registered source.
- **`build_vector_sources` takes `repositories.sets.ItopRepositories` directly,
  no protocol wrapping it** (`TASK-038`, `ADR-022`): one real implementation,
  a surface already as narrow as `for_principal` alone — a protocol here would
  duplicate it for no reader's benefit. It is the same class every other
  consumer of iTop repositories imports, not a second declaration local to
  this package.
- Every registered family is built unconditionally by `build_vector_sources`,
  with `classes` read fresh from `cfg.families` on every call — not once at
  startup — so the admin UI's chunking vocabulary (`GET /api/vector/sources`)
  survives a family being emptied or removed from the saved config by
  mistake, and a family added or removed live applies without a restart
  (TASK-021).
- iTop access itself follows `.claude/rules/itop.md` like any other
  repository consumer — nothing here is exempt from "nothing outside the
  repositories touches iTop".
