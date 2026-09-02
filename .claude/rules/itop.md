---
paths:
  - "assistant/src/itop_ai_assistant/itop_client/**"
  - "assistant/src/itop_ai_assistant/repositories/**"
  - "assistant/src/itop_ai_assistant/itop/**"
  - "assistant/src/itop_ai_assistant/domain/**"
  - "assistant/src/itop_ai_assistant/content_sources/**"
  - "assistant/src/itop_ai_assistant/agents/**/tools.py"
  - "assistant/test/**/test_itop_*.py"
  - "assistant/test/**/test_*repository*.py"
---

# Working with iTop

Full REST/JSON reference (operations, key formats, `output_fields`, case log
formats, pagination, object classes): `dev-docs/reference/itop-api.md`.

## `itop_client/` is a vendored external library

A fork of itoptop, rewritten with httpx. Keep it self-contained and generic: no
imports from this application, and **do not remove functionality this service
happens not to use**. Application-specific logic belongs in
`repositories/`.

## Nothing outside the repositories touches iTop

No exceptions — `ai_person_name()` was the last one and became
`IdentityRepository` in TASK-027.

Tools and agents never see the raw client or an iTop attribute name. All access
goes through `repositories/` — **one** `ObjectRepository` translates semantic
fields to attributes for every object family, driven by the family's `Schema`
(`domain/schema.py`, ADR-034) and its mapping section (`fields`,
`class_overrides`). How a raw value reads is the field's `kind`, not a decision
written per field; a field the deployment does not map is **absent** from the
`ObjectView`, so a typed model over it falls back to its own default rather
than to `""`. `TicketRepository` is a thin typed view over it — `Ticket` exists
because `intake` reads those names as identifiers; a family that lives only in
the index gets no class at all. `CatalogRepository` reads the service catalog;
`AccessRepository` is narrower, one read of the calling principal's own access
scope; `IdentityRepository` answers who the connection is.

Generic code holds an `ObjectView` and asks it by semantic name and kind
(`text`, `identifier`, `identifiers`, `state`, `moment`, `log`) or by meaning
(`state_of(Role.LIFECYCLE_STATE)`, `moment_of(Role.MODIFIED_AT)`) — never
`getattr`. A name the family does not declare raises; reading a field as the
wrong kind raises.

**A module names the fields it cannot work without.** `TicketRun.required_fields`
is checked against the deployment's mapping before the object is read, and an
unmapped one skips the run with a journal step naming it — mypy checks the
usage, the run checks the presence.

Repositories are **stateless** — a client and a mapping, no caches. A cache
belongs to whoever owns the event that invalidates it (the name cache lives on
`ItopConnection`, which is what rebuilds the client).

A run receives repositories only as a `RepositorySet` from
`ItopRepositories.for_principal()`, never one built by hand: the set is what
makes "one run, one identity" structural rather than a convention. Object
families live in `RepositorySet.objects`, keyed by family name — a new family
is an entry there, not a new field. OQL templates
use semantic `:this->field` placeholders bound from `ticket.model_dump()`.
Adapting to a customized iTop datamodel must stay a config change, not a code
change.

Processing code works with the semantic `Ticket` model (`domain/ticket.py`) —
`subcategory_id`, `caller_name`, `ticket.label`, `ticket.service_id` — never
with raw dicts. Distinct iTop classes get distinct models
(`Service` / `ServiceSubcategory`).

## Response pitfalls

- Check `code == 0` **before** reading `objects` — any other code is an error.
- `objects` is `null`, not `{}`, when nothing matched.
- Linked-object values arrive as dicts (`{"id": 5, "name": "Foo"}`), not strings.
- `core/update` has no bulk form — one object per call.
- Pages start at **1**, not 0.
- Case logs are append-only: use `add_item`, never rewrite `items`.
- Avoid `*_list` link-set fields in `output_fields` unless needed.

## Writing

- Questions to the user → `public_log`; notes for engineers → `private_log`.
- Act only while the ticket is in an `active_status` (see `intake` config); if
  an engineer has taken it, stop silently.
- Every write carries the run's `comment` (module, run id, delegated engineer) —
  it lands in the object's History. Never post without it from a run.
- **Dry run**: when section `platform` has `dry_run` on, `for_principal` hands
  out a client view that drops everything but a read (`Itop.read_only`), so a
  module that has never heard of the mode cannot write either. Never check the
  mode in a tool or a module — asking every future tool to remember is the
  arrangement the ban exists to replace (REQ-006 R2), and telling the model
  would show the customer behaviour production does not have (R3).
  `provision_itop` builds its client from `create_itop_client` and is
  deliberately outside this seam: the mode is switched on before the
  installation is finished.
