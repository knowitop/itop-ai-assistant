---
paths:
  - "assistant/src/itop_ai_assistant/config.py"
  - "assistant/src/itop_ai_assistant/settings/config_store.py"
  - "assistant/src/itop_ai_assistant/admin/**"
  - "assistant/src/itop_ai_assistant/settings/ui_hints.py"
  - "assistant/src/itop_ai_assistant/agents/*/config.py"
---

# Configuration

Priority (high → low): Redis runtime overrides (setup/admin API) → env vars →
`.env` → `config.yaml` → field defaults. Every variable is tabulated in
`docs/configuration.md`; `docker/.env.dist` is the full template.

**No field is required at startup** — the app always boots. Until the `itop` and
`llm` sections are complete, `/webhook` returns 503 and the admin API stays open
for the setup wizard. Connection edits apply from the next run without a
restart; the exceptions are `intake.enabled` / `intake.classes` and
`selfcheck.enabled`, which are read at startup because the trigger registry is
built from them.

**Which layer a new switch belongs to** — two questions, in this order.

*Where is the thing it switches created?* Once per process (the tracing
instrumentation, the Redis and Qdrant clients) and the switch is env/`Settings`:
a runtime section could not turn it off without a teardown path that does not
exist, and tracing's packages are not even installed when it is off (ADR-029).
Created per run or per tick (the telemetry sender, `dry_run`, the LLM client)
and a runtime section is the natural home. The `itop` section is the standing
exception: its client is built once and rebuilt on a fingerprint change, because
the setup wizard has to configure iTop inside a container nobody can restart
from within.

*Who reaches for it, and against whom?* A setting that configures the
installation's own infrastructure can live in env, where only whoever deployed
it will look. A setting that limits what **we** do — telemetry (REQ-009), the
dry run (REQ-006) — belongs in the admin UI: making it harder to reach than the
thing it restrains is a conflict of interest, and the person who wants it off is
often not the person with shell access. Env may still seed such a setting's
value at deploy time; it does not replace the control.

A module's config model is also what the admin form is built from: `title` and
`description` on a field are what an administrator reads, and the section a
field belongs to is declared with `ui()` from `settings/ui_hints.py`, never as
a literal `json_schema_extra` (ADR-025).
