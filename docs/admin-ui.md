# Admin UI

The assistant ships with a built-in admin UI at `http://localhost:8001/ui`. The UI requires the admin token (entered once, stored in the browser's localStorage).

The interface is available in 12 languages — use the language selector in the top-right corner. The **Modules** screen is translated by the modules themselves, so a module that ships no file for the chosen language shows its own English there while the rest of the interface follows your choice — see [Translating a module's settings](#translating-a-modules-settings).

While [Dry run](configuration.md#dry-run) is on, a yellow **dry run** badge sits in the header of every screen: nothing the assistant does is reaching iTop.

---

## Setup

The first-run wizard ([detailed walkthrough](setup.md#setup-wizard)). On a configured system, this screen shows a status summary — green if `/webhook` is active, or a list of what is still missing with a shortcut to re-run the wizard.

---

## Connections

Fine-grained editing of every connection section after initial setup — iTop, LLM, embeddings, security and the iTop datamodel mapping. Changes apply from the next processed ticket without a restart, and each section can be reset to its environment defaults.

### iTop tab

- **REST API URL**, API version, request timeout
- **Auth method** — application token or username + password; secrets are write-only (shown as "set" / "not set")
- **Test connection** — probes the values currently in the form without saving anything; secrets you have not retyped are taken from the stored config, so you can re-test without re-entering the password. A successful probe returns the login of the account behind the credentials — check that it is the AI service account
- **iTop webhooks** — the same provisioning form as in the setup wizard step 3; useful for re-provisioning after a URL or token change

### LLM tab

- **Provider** — how the model is reached (`openai_compatible`, `openai`, `google_genai`, `ollama`). The form follows the choice: fields the provider does not use are hidden, and the base-URL placeholder changes with it. See [supported providers](configuration.md#supported-llm-providers)
- **Base URL**, model name, API key — shown only where the selected provider needs them
- **Model parameters** — free-form JSON forwarded to the client as-is: `{"temperature": 0.2, "max_tokens": 2048}`. Connection fields (`model`, `base_url`, `api_key`) are rejected here — they have their own inputs
- **Endpoint accepts a forced `tool_choice`** — visible only for `openai_compatible`, where the answer depends on the actual server behind the URL (vLLM and LiteLLM accept it, DeepSeek returns HTTP 400). For the named providers the answer is built in and there is nothing to set. See [Forced tool calls](configuration.md#forced-tool-calls)
- **Think tags** — tag names stripped from model responses as reasoning blocks (default: `think`, `thinking`, `reasoning`); relevant for reasoning models like DeepSeek-R1 or Qwen3
- **Test LLM** — sends a test request and reports three things: that the model answered, that it can call a tool, and — when forcing is switched on — whether the endpoint accepted the forced `tool_choice`

Model parameters and the `tool_choice` switch live here only, not in the setup wizard: the wizard gets you to a working state, fine-tuning belongs in Connections.

### Embeddings tab

Only needed for the optional [vector index](#vector-index). Leave it empty and intake works as it always has, with one thing missing: the handoff note carries no references to similar solved tickets.

- **Base URL**, model, API key — an OpenAI-compatible `/v1/embeddings` endpoint. The model must be **multilingual** (tickets are usually mixed-language), e.g. `bge-m3`
- **Dimension** — must match what the model actually returns
- **Batch size** — texts per request (default `32`)
- **Test embeddings** — embeds a probe text, reports the endpoint's real vector dimension and whether it matches the Dimension field

### Security tab

- **Webhook Token** and **Admin Token** — write-only fields with generate, copy and clear buttons
- Clearing the admin token puts the API back into open (unauthenticated) mode — a confirmation is required

### Data model mapping tab

How our semantics map onto your iTop datamodel — edit this instead of the code when iTop has been customized. Two sections, **Tickets** and **FAQ**, with the same form: one row per semantic field, the iTop attribute code on the right, and a **No such attribute** switch for what your datamodel does not have.

- The row list comes from the model itself, so it always shows every field the assistant can use
- The switch stores `null`. It matters: an attribute left mapped but missing from iTop makes the request fail — this is how indexing of the FAQ family breaks on a datamodel without `error_code`
- Saving writes the whole section, so nothing you left alone is lost
- **Per-class overrides** (tickets only) — point differences between the ticket classes, merged over the fields above, e.g. `{"Incident": {"request_type": null}}`. A JSON editor; an unknown semantic name is rejected with the server's message
- **Reset to defaults** puts the section back to what env/yaml say

---

## Modules

At the top of the screen — **Dry run**, the one switch that applies to the whole installation rather than to a module: with it on, every module keeps running exactly as it would in production, but nothing is written to iTop. It belongs here because it is about what modules may do with iTop, not about which iTop they talk to. What it does and does not prove is in [Dry run](configuration.md#dry-run).

Below it, per-module business settings. The form is built from the module's own settings schema, so a module's fields appear here as soon as the module exists — nothing about them is hard-coded in the UI.

Fields are grouped the way the module groups them. The settings that apply to the module as a whole come first; below them, one section per action the module can perform, each headed by the switch that turns that action on. Switching a section off greys out its settings rather than hiding them, so it stays visible what is being saved. Settings an administrator rarely touches — OQL templates, similarity thresholds — are folded away behind **Advanced settings** inside their section.

Every field carries its own label and explanation from the module, and rejected values are reported on the field they belong to; a rule that spans two fields (for instance, "references in one note" not exceeding "candidates read from the index") is reported above the form, where it belongs.

The full list of intake's settings, with defaults, is in [Configuration](configuration.md#intake-module-settings).

### Translating a module's settings

Labels, explanations and section headings on this screen come from the module, not from the interface's own translation files: the module ships them as `locales/<lang>.json` next to its settings model, and the assistant applies them when the screen asks for that language.

Two consequences worth knowing:

- **A missing translation is never an error.** A language the module ships no file for, or a field the file does not mention, falls back to the module's English. Nothing else on the screen changes.
- **A module you add yourself is translatable without touching the UI.** Put `locales/ru.json` in the module's package with the field names as keys:

  ```json
  {
    "description": "What the module does, one line",
    "groups": { "Classification": "Классификация" },
    "fields": {
      "max_questions": {
        "title": "Вопросов заявителю",
        "description": "Сколько раз модуль может написать заявителю."
      }
    },
    "actions": { "process": { "summary": "Обработать одну заявку сейчас" } },
    "schedules": { "tick": { "summary": "Запуск по таймеру" } }
  }
  ```

  English needs no file — it is the `title`/`description` of the settings model itself.

Changes apply from the next processed ticket — no restart needed, **except Enabled and Classes**, which are read at startup. Each module can be reset to its defaults.

Below the settings a module shows what may start it besides an iTop event:

- **Schedule** — what the clock runs on its own, with the period taken from the module's own settings. Read-only here.
- **Run manually** — a synchronous run started from this screen; the answer comes back into the page.

The **Selfcheck** module (disabled by default, `selfcheck.enabled`) has both. It writes nothing anywhere: it reads the service catalog through the iTop connection, asks the model to say hello, and records both in the run journal. Turn it on when you want proof that a deployment's iTop and model connections work under real module code rather than under a wizard probe.

---

## Prompts

View and edit the LLM prompts used by each module. Overridden prompts are flagged in the sidebar.

The **Intake** module has the following prompts — the three messages that open
the agent's session:

| Prompt | Purpose |
|--------|---------|
| `system` | Who the agent is, the rules it works under, when to ask versus when to hand off |
| `catalog_human` | The service catalog available to the requester's organization (sent only for an unclassified ticket) |
| `ticket_human` | This ticket: title, description, current classification, conversation so far |

Edit a prompt in the textarea and click **Save** — the change takes effect from the next processed ticket, no restart needed. Any prompt can be reset to its packaged default with **Reset to default**.

Placeholder validation runs on save: if a template references an unknown variable, the error is shown before the change is stored. See [Customizing prompts](prompts.md) for the full list of available placeholders.

---

## Runs

The processing journal — a filterable list of every run the assistant has made, whatever started it.

- **Filter by subject** — what the run was about: a ticket reference like `UserRequest::123`, or a module's own subject for a scheduled run like `selfcheck` (exact match)
- **Filter by status** — `running`, `done`, or `failed`
- The list auto-refreshes every 5 seconds while any run is in progress

Click a row to see the step-by-step timeline of the agent session:

| Step | What it shows |
|------|---------------|
| `lock` / `guard` | Why a run stopped before reaching the model (already processed, engineer assigned, last comment was ours) |
| `agent` | One model turn: which tools it called and with which arguments, or the text it wrote when it called nothing |
| `tool:<name>` | The result of that call — `[success]` or `[error]` plus the text the tool sent back to the model |
| `usage` | Model calls, tokens in/out and wall time for the whole run |

An `agent` step carries the arguments in full and unshortened — the whole text of the question or of the handoff note, exactly as it would have been posted; a `tool:set_classification` step names the service and the subcategory the assistant picked, not only their ids. On a dry run this is the product rather than diagnostics, which is what it is written for.

A run made while [Dry run](configuration.md#dry-run) was on carries a **dry run** badge in the list and in its own panel, and keeps it after the mode is switched off.

Failed runs show the full error text.

The `processing_id` returned by `POST /webhook` can be used to find the exact run — the interactive API docs at `http://localhost:8001/docs` describe all available endpoints.

---

## Vector index

Optional — the screen only does something when `QDRANT_URL` points at a Qdrant instance. One thing reads the index today: intake, for the similar solved tickets it quotes in the handoff note (see [intake settings](configuration.md#intake-module-settings)). The rest of what is indexed here waits for features still to come.

**Status tab** — badges for the vector store, embeddings and indexer state; the active index version with row count; the per-class sweep cursors and the last reconciliation; and a table of recent indexing runs (objects seen, chunks embedded, chunks with metadata refreshed, chunks deleted, duration). "Chunks with metadata refreshed" counts chunks whose text was unchanged but whose status/org/filters were rewritten without a re-embed. **Index now** runs the next ordinary pass immediately instead of waiting out the sweep interval: only objects changed since the last pass are re-embedded, which is what makes it cheap enough to press after editing the settings. **Reindex** schedules a full rebuild — every object is re-embedded, so it can take a while and load the embeddings endpoint. A warning appears if the index was built with a different embeddings model or dimension than the current config: incomparable vectors are never mixed, so a rebuild is the only way forward.

**Indexer tab** — whether indexing is on (applies from the next sweep, no restart), the system-wide sweep interval / page size / throttle, the reconciliation interval, and the chunk budgets.

**Indexed classes tab** — one section per **family** (one Qdrant collection each — today: **tickets**, **FAQ**). A family section holds its own optional overrides for the sweep interval and the log-entries-per-chunk window (blank = use the system-wide value from the Indexer tab — useful for a source like FAQ, which has no incremental cursor and re-scans its whole class on every sweep), and its classes: which values of each class's relevance attribute keep an object in the index (empty = index everything), and what each fragment of an indexed object contains. Adding a class to a family's list is enough to index it under that family — no code change, as long as the family's source can read the class.

Each of the two tabs saves and resets on its own: resetting the indexer settings leaves the indexed classes as they are, and the other way round.

An object is indexed as several **fragments**, matched separately — a query can look like another ticket's description without looking like its solution, and keeping them apart is what makes the difference visible. Which fragments exist is decided by the indexing source, not by you: tickets have `profile`, `body`, `solution` and the two case logs. For each of the first three you pick the semantic fields that feed it (title, description, solution, service, subcategory) — that is where you adapt to a customized iTop datamodel. A fragment with no fields selected produces nothing, and the screen warns when a class ends up producing nothing at all.

The two log fragments have no fields to choose: their content is fixed by the source, so they are simply on or off. **`log:private` is marked `internal`** — it holds engineer-only notes, and switching it on means their embeddings are stored in the vector database. It is off by default on purpose. Internal fragments are never returned to searches run on behalf of a caller; that boundary is enforced in code and is not a setting.

---

## System

Housekeeping about the installation itself, as opposed to what it does with tickets (Modules) or which systems it talks to (Connections).

Today it holds one thing: **anonymous usage telemetry** — the switch, this installation's identifier, and **Show today's document**, which prints the exact JSON that would be sent today. Not an example and not a description of the format: it is produced by the same code the sender uses, so it cannot drift from what actually leaves. It answers with telemetry switched off too, which is what makes it useful before deciding.

The switch applies immediately, without a restart. A line appears when telemetry is on and still silent — that happens on a build we did not publish, which never sends whatever the switch says.

What is collected, who receives it, and how to have it deleted: [Telemetry](telemetry.md).

