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

A module's config model is also what the admin form is built from: `title` and
`description` on a field are what an administrator reads, and the section a
field belongs to is declared with `ui()` from `settings/ui_hints.py`, never as
a literal `json_schema_extra` (ADR-025).
