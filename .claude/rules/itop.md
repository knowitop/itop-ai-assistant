---
paths:
  - "assistant/src/itop_ai_assistant/itop_client/**"
  - "assistant/src/itop_ai_assistant/*_repository.py"
  - "assistant/src/itop_ai_assistant/itop_provisioning.py"
  - "assistant/src/itop_ai_assistant/domain/**"
  - "assistant/src/itop_ai_assistant/vector_sources/**"
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
`ticket_repository.py`.

## Nothing outside the repositories touches iTop

Tools and agents never see the raw client or an iTop attribute name. All access
goes through `TicketRepository` / `CatalogRepository`, which are the only place
semantic fields are translated to attributes (driven by the `ticket_mapping`
config section: `fields`, `class_overrides`, `active_statuses`). OQL templates
use semantic `:this->field` placeholders bound from `ticket.model_dump()`.
Adapting to a customized iTop datamodel must stay a config change, not a code
change.

Processing code works with the semantic `Ticket` model (`domain/ticket.py`) —
`subcategory_id`, `caller_name`, `ticket.label`, `ticket.has_service` — never
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
- Act only while the ticket is in an `active_status` (see `ticket_mapping`); if
  an engineer has taken it, stop silently.
- Every write carries the run's `comment` (module, run id, delegated engineer) —
  it lands in the object's History. Never post without it from a run.
