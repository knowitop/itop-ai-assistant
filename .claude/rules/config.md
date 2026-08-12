---
paths:
  - "assistant/src/itop_ai_assistant/config.py"
  - "assistant/src/itop_ai_assistant/settings/config_store.py"
  - "assistant/src/itop_ai_assistant/admin/**"
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
