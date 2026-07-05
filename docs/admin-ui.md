# Admin UI

The assistant ships with a built-in admin UI at `http://localhost:8001/ui`. The UI requires the admin token (entered once, stored in the browser's localStorage).

The interface is available in 12 languages — use the language selector in the top-right corner.

---

## Setup

The first-run wizard ([detailed walkthrough](setup.md#setup-wizard)). On a configured system, this screen shows a status summary — green if `/webhook` is active, or a list of what is still missing with a shortcut to re-run the wizard.

---

## Connections

Fine-grained editing of the iTop, LLM and security settings after initial setup. Changes apply immediately without a restart. Each section can be reset to its environment defaults.

### iTop tab

- **REST API URL**, API version, request timeout
- **Auth method** — application token or username + password; secrets are write-only (shown as "set" / "not set")
- **Test connection** — probes the current saved settings without saving changes
- **iTop webhooks** — the same provisioning form as in the setup wizard step 3; useful for re-provisioning after a URL or token change

### LLM tab

- **Base URL**, model name, API key
- **Think tags** — tag names stripped from model responses as reasoning blocks (default: `think`, `thinking`, `reasoning`); relevant for reasoning models like DeepSeek-R1 or Qwen3
- **Test LLM** — sends a test request to verify the model responds

### Security tab

- **Webhook Token** and **Admin Token** — write-only fields with generate, copy and clear buttons
- Clearing the admin token puts the API back into open (unauthenticated) mode — a confirmation is required

---

## Modules

Per-module business settings. Currently the **Enrichment** module exposes:

| Setting | Default | Description |
|---------|---------|-------------|
| Enabled | `true` | Enable or disable the module entirely |
| Classes | `UserRequest`, `Incident` | Ticket classes the module handles |
| Max rounds | `2` | Maximum completeness clarifying questions per ticket |
| Max classify rounds | `2` | Maximum classification clarifying questions per ticket |
| Classify model | _(global)_ | Override LLM model for the classification node |
| Evaluate model | _(global)_ | Override LLM model for the evaluation node |
| Enrich model | _(global)_ | Override LLM model for the enrichment node |

Changes apply from the next processed ticket — no restart needed. Each module can be reset to its defaults.

---

## Prompts

View and edit the LLM prompts used by each module. Overridden prompts are flagged in the sidebar.

The **Enrichment** module has the following prompts:

| Prompt | Purpose |
|--------|---------|
| `classify_service` | Select the best matching Service from the iTop catalog |
| `classify_subcategory` | Select the best matching ServiceSubcategory |
| `classify_ask` | Post a clarifying question if the category cannot be determined confidently |
| `evaluate` | Decide whether the ticket description is sufficient |
| `ask` | Post a clarifying question if the description is incomplete |
| `enrich` | Generate a structured internal note for the engineer |

Edit a prompt in the textarea and click **Save** — the change takes effect from the next processed ticket, no restart needed. Any prompt can be reset to its packaged default with **Reset to default**.

Placeholder validation runs on save: if a template references an unknown variable, the error is shown before the change is stored. See [Customizing prompts](prompts.md) for the full list of available placeholders.

---

## Runs

The processing journal — a filterable list of every ticket the assistant has handled.

- **Filter by ticket** — enter a ticket reference like `UserRequest::123` (exact match)
- **Filter by status** — `running`, `done`, or `failed`
- The list auto-refreshes every 5 seconds while any run is in progress

Click a row to see the step-by-step timeline: which processing node ran, when, and what it did. Failed runs show the full error text.

The `processing_id` returned by `POST /webhook` can be used to find the exact run — the interactive API docs at `http://localhost:8001/docs` describe all available endpoints.
