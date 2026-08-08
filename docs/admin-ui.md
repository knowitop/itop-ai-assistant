# Admin UI

The assistant ships with a built-in admin UI at `http://localhost:8001/ui`. The UI requires the admin token (entered once, stored in the browser's localStorage).

The interface is available in 12 languages — use the language selector in the top-right corner.

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

Only needed for the optional [vector index](#vector-index) — leave it empty for a plain intake deployment.

- **Base URL**, model, API key — an OpenAI-compatible `/v1/embeddings` endpoint. The model must be **multilingual** (tickets are usually mixed-language), e.g. `bge-m3`
- **Dimension** — must match what the model actually returns
- **Batch size** — texts per request (default `32`)
- **Test embeddings** — embeds a probe text, reports the endpoint's real vector dimension and whether it matches the Dimension field

### Security tab

- **Webhook Token** and **Admin Token** — write-only fields with generate, copy and clear buttons
- Clearing the admin token puts the API back into open (unauthenticated) mode — a confirmation is required

### Ticket mapping tab

How semantic ticket fields map onto your iTop datamodel — edit this instead of the code when iTop has been customized:

- `fields` — semantic name → iTop attribute code (`null` = the attribute does not exist)
- `class_overrides` — per-class differences (e.g. `Incident` has no `request_type`)
- `active_statuses` — the statuses in which the assistant is allowed to act

---

## Modules

Per-module business settings. Currently the **Intake** module exposes:

| Setting | Default | Description |
|---------|---------|-------------|
| Enabled | `true` | Enable or disable the module entirely |
| Classes | `UserRequest`, `Incident` | Ticket classes the module handles |
| Max rounds | `2` | Maximum completeness clarifying questions per ticket |
| Max classify rounds | `2` | Maximum classification clarifying questions per ticket |
| Max iterations | `8` | Budget of model calls per ticket; on exhaustion the run is closed with the fallback note |
| Model | _(global)_ | Override LLM model for the module — it must call tools reliably |
| Classify fallback note | _(see [Configuration](configuration.md#intake-module-settings))_ | Internal note when the ticket stays unclassified |
| Handoff fallback note | _(see Configuration)_ | Internal note when the agent ends without a question or a handoff |

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

Failed runs show the full error text.

The `processing_id` returned by `POST /webhook` can be used to find the exact run — the interactive API docs at `http://localhost:8001/docs` describe all available endpoints.

---

## Vector index

Optional — the screen only does something when `QDRANT_URL` points at a Qdrant instance. This is infrastructure for upcoming semantic-search features; nothing in the current intake flow reads the index.

**Status tab** — badges for the vector store, embeddings and indexer state; the active index version with row count; the per-class sweep cursors and the last reconciliation; and a table of recent indexing runs (objects seen, chunks embedded, chunks with metadata refreshed, chunks deleted, duration). "Chunks with metadata refreshed" counts chunks whose text was unchanged but whose status/org/filters were rewritten without a re-embed. **Index now** runs the next ordinary pass immediately instead of waiting out the sweep interval: only objects changed since the last pass are re-embedded, which is what makes it cheap enough to press after editing the settings. **Reindex** schedules a full rebuild — every object is re-embedded, so it can take a while and load the embeddings endpoint. A warning appears if the index was built with a different embeddings model or dimension than the current config: incomparable vectors are never mixed, so a rebuild is the only way forward.

**Settings tab** — whether indexing is on (applies from the next sweep, no restart), the sweep interval / page size / throttle, the reconciliation interval, chunk token budget, and per-class settings: which values of the class's relevance attribute keep an object in the index (empty = index everything), and what each fragment of an indexed object contains.

An object is indexed as several **fragments**, matched separately — a query can look like another ticket's description without looking like its solution, and keeping them apart is what makes the difference visible. Which fragments exist is decided by the indexing source, not by you: tickets have `profile`, `body`, `solution` and the two case logs. For each of the first three you pick the semantic fields that feed it (title, description, solution, service, subcategory) — that is where you adapt to a customized iTop datamodel. A fragment with no fields selected produces nothing, and the screen warns when a class ends up producing nothing at all.

The two log fragments have no fields to choose: their content is fixed by the source, so they are simply on or off. **`log:private` is marked `internal`** — it holds engineer-only notes, and switching it on means their embeddings are stored in the vector database. It is off by default on purpose. Internal fragments are never returned to searches run on behalf of a caller; that boundary is enforced in code and is not a setting.
