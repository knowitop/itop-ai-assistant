# Configuration

Settings resolve in priority order: **runtime overrides (setup API / admin UI, stored in Redis) → environment variables → `.env` → `assistant/config.yaml` → built-in defaults**.

Environment variables are the IaC-friendly path; the setup API edits the same settings at runtime without a restart. Only the bootstrap values (`REDIS_URL`, `QDRANT_URL`, `LOG_LEVEL`, `PROMPTS_DIR`, `UI_DIST_DIR`) are env-only and require a restart to change.

A full `.env` template with examples is in [`docker/.env.dist`](https://github.com/knowitop/itop-ai-assistant/blob/main/docker/.env.dist). `assistant/config.yaml` is committed to the repository and holds non-secret defaults — it is the convenient place for structured values like `LLM_PARAMS`, which env vars can only express as a JSON string.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ITOP_URL` | yes — env or setup API | iTop REST API URL |
| `ITOP_USER` + `ITOP_PWD` | one of — env or setup API | iTop basic auth — use this or `ITOP_TOKEN` |
| `ITOP_TOKEN` | one of — env or setup API | iTop application/personal token — use this or basic auth |
| `ITOP_API_VERSION` | default `1.3` | iTop REST API version |
| `ITOP_TIMEOUT` | default `30.0` | HTTP timeout in seconds for iTop requests |
| `LLM_PROVIDER` | default `openai_compatible` | How the model is reached — see [Supported LLM providers](#supported-llm-providers) |
| `LLM_BASE_URL` | depends on provider | Endpoint URL; unused by `openai` and `google_genai` |
| `LLM_MODEL` | yes — env or setup API | Model name as exposed by the endpoint |
| `LLM_API_KEY` | depends on provider | Required by cloud providers; local servers ignore it |
| `LLM_PARAMS` | optional | JSON passed to the client as-is: `{"temperature": 0.2, "max_tokens": 2048}` |
| `LLM_SUPPORTS_FORCED_TOOL_CHOICE` | optional | Only for `openai_compatible` — see [Forced tool calls](#forced-tool-calls) |
| `LLM_THINK_TAGS` | default `["think","thinking","reasoning"]` | JSON list of tag names stripped from responses as reasoning blocks |
| `DRY_RUN` | default `false` | Dry run: runs happen, nothing is written to iTop — see [Dry run](#dry-run) |
| `WEBHOOK_TOKEN` | recommended | Shared secret for `/webhook`; iTop must send it in `X-Auth-Token`. Unset = unauthenticated |
| `ADMIN_TOKEN` | recommended | Bearer token for `/api` admin endpoints. Unset = open (first-run mode) |
| `REDIS_URL` | default `redis://localhost:6379` | Redis connection URL (env-only, requires restart); the compose stack sets `redis://redis:6379` |
| `STATE_TTL_DAYS` | default `30` | How long per-ticket AI state lives in Redis |
| `RUN_TTL_DAYS` | default `7` | How long the run journal keeps a processing run |
| `QDRANT_URL` | optional | Qdrant URL for the vector index (`http://host:6333`), env-only. Unset = Redis-only — see [Vector index](#vector-index) |
| `EMBEDDINGS_BASE_URL` / `EMBEDDINGS_MODEL` / `EMBEDDINGS_API_KEY` | optional | OpenAI-compatible embeddings endpoint for the vector index. `EMBEDDINGS_BASE_URL` must be the full prefix your provider documents, not a bare host:port — for Ollama that's `http://localhost:11434/v1` (not the bare `host:port` used for the LLM chat connection), for Google's Gemini API it's `https://generativelanguage.googleapis.com/v1beta/openai` |
| `EMBEDDINGS_DIMENSION` | default `1024` | Vector dimension — must match what the model returns |
| `EMBEDDINGS_BATCH_SIZE` | default `32` | Texts per embeddings request |
| `TELEMETRY_ENABLED` | default `true` | Send one anonymous document a day about this installation — see [Telemetry](telemetry.md). Editable at runtime; a build you compiled yourself never sends regardless |
| `TELEMETRY_TEST_MODE` | default `false` | Marks this installation's signals as test ones. For verification stands — see [Telemetry](telemetry.md#builds-that-never-send-anything) |
| `TELEMETRY_ALLOW_UNPUBLISHED_BUILD` | default `false` | Lets a build we did not publish report anyway — set it if you deployed from source onto a real server and want to be counted. See [Telemetry](telemetry.md#builds-that-never-send-anything) |
| `TRACING_ENABLED` | default `false` | Send LLM traces to a self-hosted receiver (env-only, requires restart) — see [LLM tracing](#llm-tracing) |
| `TRACING_ENDPOINT` | default `http://localhost:6006/v1/traces` | OTLP/HTTP endpoint of the trace receiver, full path to `/v1/traces` (env-only) |
| `TRACING_PROJECT_NAME` | default `itop-ai-assistant` | Project the traces are filed under in the receiver (env-only) |
| `PROMPTS_DIR` | optional | Directory with prompt file overrides (env-only) — see [Customizing prompts](prompts.md) |
| `UI_DIST_DIR` | optional | An admin SPA to serve instead of the one shipped inside the package (env-only). Only needed by a deployment that builds its own |
| `LOG_LEVEL` | default `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (env-only) |

> [!NOTE]
> Runtime overrides (including secrets set through the setup API) live in Redis. The bundled `docker-compose.yml` enables Redis persistence (`appendonly yes` + volume) so they survive restarts. To recover a lost admin token, set `ADMIN_TOKEN` in `.env` and restart, or delete the `config:security` key in Redis.

---

## Dry run

`DRY_RUN` (section `platform`, editable at runtime through the admin UI or `PATCH /api/setup/platform`) puts the whole installation into a mode where the assistant does everything it would normally do — reads the service catalogue, classifies the ticket, decides whether to ask or to hand over, searches for similar solved tickets, records every step in the run journal — but **nothing reaches iTop**: no field change, no public log entry, no private note.

This is what to switch on before letting the assistant act on a live queue: see what it would have done on your own tickets, then decide. The setup wizard keeps working with the mode on, so it can be switched on before the installation is finished.

- Applies **from the next run** — no restart, and a run already in flight finishes as it started.
- Every run processed this way is marked in the [Runs](admin-ui.md#runs) screen and stays marked after the mode is switched off; the admin UI also shows a `dry run` badge on every page while it is on.
- Per-ticket state is kept exactly as in production — the question budget is spent, the ticket is marked as processed. A ticket the assistant has looked at during a dry run is **not** picked up again once the mode is off.
- Everything except the writes stays on deliberately, including the vector index: a mode that also disabled indexing would test a system you are not going to run.

What the mode cannot show you is described in [Setup](setup.md#try-it-on-your-own-data-first).

## LLM tracing

The run journal in the admin UI records what the assistant did — which tools it called, with which arguments, what came back. It does not record **what was sent to the model and what the model answered**, which is what the question "why did it decide that?" actually needs. Tracing adds exactly that, and keeps it inside your perimeter.

Off by default. Turning it on takes two things: a receiver, and `TRACING_ENABLED=true` on the assistant.

```bash
# in docker/, with TRACING_ENABLED=true in .env
docker-compose --profile tracing up -d
```

The bundled stack runs [Phoenix](https://arize.com/docs/phoenix/self-hosting) — one container, its own volume, UI on `http://localhost:6006`. It is behind a compose profile, so a stand that is not tracing never starts it.

> [!IMPORTANT]
> **Traces contain the full text of tickets** — the description, the requester's comments, everything the model was shown. That is the point of them, and it makes the receiver a second long-lived store of personal data next to iTop itself. Three questions have to have answers before this is switched on anywhere near real users: where the receiver runs, who can open it, and after how many days traces are deleted. Nothing here is a substitute for asking them.

- **Where it goes is yours to choose.** `TRACING_ENDPOINT` is a plain OTLP/HTTP address, so any receiver that speaks OTLP fits in the same slot with no change to the assistant. Write the full path to `/v1/traces` — a bare `host:port`, or a gRPC port such as `:4317`, exports nothing at all.
- **Retention is set on the receiver, not here.** The bundled Phoenix is started with `PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=30`; its own default is to keep traces forever.
- **This is not the run journal, and does not replace it.** The journal lives in Redis for `RUN_TTL_DAYS` (7 by default) and is what the [Runs](admin-ui.md#runs) screen reads. Traces outlive it deliberately: a ticket usually closes long after the run that touched it, and comparing the two is only possible if the run's reasoning is still around. Both are keyed by the same `processing_id`, so a run in the admin UI and its trace find each other.
- **Nothing is installed or connected while it is off.** With `TRACING_ENABLED=false` the tracing packages are not even imported, no exporter exists and no connection is made.
- **If the receiver is unreachable while tracing is on**, ticket processing is unaffected — spans are exported in the background — but the log fills with export retry warnings. Fix the endpoint or switch tracing off.

### The cloud path

LangChain also honours `LANGSMITH_TRACING` / `LANGSMITH_ENDPOINT` / `LANGSMITH_API_KEY` with no code involved on our side, and `docker/.env.dist` still ships them, off. That path sends ticket text to a third party and stores it there. It is a reasonable choice for development against synthetic data, and not one to make by accident on someone's live queue — a customer's agreement to a cloud *model* is not agreement to a cloud *trace store*.

## Build version

The running build identifies itself at `GET /version` (public, like `/health`),
in the first line of the startup log, and at the bottom of the admin sidebar.

Nothing needs to be configured. The stamp is baked into the package when it is
built and read back through the distribution metadata, so the artifact
describes itself and no deployment setting can change what it reports.

Where the value comes from depends on who is building:

| build | version | commit |
|-------|---------|--------|
| the release workflow, from tag `v0.3.0` | `0.3.0` — the same string as the published image tag | the released commit |
| a checkout (`uv sync`, `uv build`) | derived from git, e.g. `0.3.1.dev8` | current `HEAD` |
| `docker build` without arguments | `0.0.0` | none |

The image build has no repository to read — `.git` is not in the build context
— so the release workflow hands the values over as build arguments. To get the
same from a local image build, pass them yourself:

```bash
docker build -f assistant/Dockerfile \
  --build-arg APP_VERSION="$(git describe --tags --abbrev=0 | sed 's/^v//')" \
  --build-arg BUILD_COMMIT="$(git rev-parse HEAD)" .
```

## Intake module settings

`intake` is the ticket-processing module: it classifies the ticket, asks at most one clarifying question at a time, and hands the ticket to an engineer with an internal note — all as a single tool-calling agent.

Set in the [Admin UI → Modules](admin-ui.md#modules) or via `PUT /api/config/intake`.

| Setting | Default | Description |
|---------|---------|-------------|
| **Module** | | |
| `enabled` | `true` | Enable or disable the intake module |
| `classes` | `["UserRequest"]` | Ticket classes to process |
| `active_statuses` | `["new"]` | Statuses in which the module is allowed to act on a ticket |
| `max_questions` | `3` | How many clarifying questions the requester gets for one ticket, in total |
| `max_iterations` | `9` | Budget of model calls per ticket; on exhaustion the run is closed with the fallback note |
| `model` | _(global LLM model)_ | Override model for the whole module — the agent needs reliable tool calling |
| **Classification** | | |
| `classify_enabled` | `true` | Let the module set the service and the subcategory of the ticket |
| `max_classify_questions` | `2` | How many of `max_questions` may be spent while the ticket is still unclassified — must not exceed it |
| `unclassified_service_ids` | `[]` | Numeric IDs of the services that stand for a missing classification — see [Services that mean "not classified"](#services-that-mean-not-classified) |
| **Clarification** | | |
| `clarify_enabled` | `true` | Let the module ask the requester clarifying questions in the public log |
| **Handoff note** | | |
| `handoff_note_enabled` | `true` | Let the module write the internal note for the engineer |
| `handoff_fallback_note` | `AI intake finished without a summary. Manual review required.` | Internal note when the agent ends without a question or a handoff |
| **Similar solved tickets** | | |
| `similar_enabled` | `true` | Let the module quote similar solved tickets in that note — requires `handoff_note_enabled` |
| `resolved_statuses` | `["resolved", "closed"]` | Ticket statuses eligible to be quoted as "similar solved tickets" |
| `similar_max_age_days` | `365` | How far back solved tickets may be quoted in the handoff note |
| `similar_candidates` | `15` | Candidates read from the index before iTop is asked which of them the run may see |
| `similar_top` | `5` | Max references in one handoff note — must not exceed `similar_candidates`, since candidates are only ever dropped by that visibility check, never added |
| `similar_min_score` | `0.6` | Minimum Qdrant cosine score a candidate must reach to be quoted, regardless of rank; a conservative starting value, not calibrated to any specific embeddings model — tune it per deployment |
| `similar_chunk_kinds` | `["profile", "body"]` | Which chunk kinds the query (title + description) is matched against; `solution` is left out by default — a match there means "the solution reads like the problem", usually noise |

> [!NOTE]
> The `similar_*` thresholds only do something when `similar_enabled` is on **and** the [vector index](#vector-index) is switched on with an embeddings endpoint configured. Without any of that, the agent is not given the search tool at all and the handoff note carries no references.

### Services that mean "not classified"

Service and subcategory are mandatory in iTop, so a ticket that nobody classified still arrives with both filled in. A mail gateway has to put *something* there and puts the same thing every time — a service like "Mail request" with a subcategory like "Other". To the module that ticket looks classified, and the one channel where no classification exists at all is the one it skips.

List those services in `unclassified_service_ids` and the module reads them as an empty field: the ticket is classified as any other, and the service itself is never offered to the model, so it cannot be classified back into it.

Subcategories are not listed. A subcategory belongs to exactly one service, so the ones under a declared service are covered along with it — the mail gateway's "Other" needs no entry of its own.

The setting takes **numeric IDs**, not names: a renamed service keeps working, and a name typed here would save cleanly and never match anything. To find the ID, open the service in iTop and read `id=` from the address bar:

```
https://itop.example.com/pages/UI.php?operation=details&class=Service&id=7
                                                                       ↑
```

A value that is not a number is rejected when you save it (422 from the admin API).

Run journal: the `classification` step of every run records what the ticket arrived with — `service=unclassified(7) subcategory=70` for the case above, `service=none subcategory=none` for a genuinely empty ticket, plain IDs for a real classification.

### Which actions the module performs

The four `*_enabled` settings above switch intake's four actions independently. A switched-off action is not "asked to be skipped": the corresponding tool is never handed to the model, so it cannot happen — and neither can the model waste a call trying. Changes apply from the next ticket, no restart needed.

Deployments differ in what they have already automated and what they are willing to let an AI do, so the useful combinations are:

| Mode | Settings | What the ticket gets |
|------|----------|----------------------|
| Everything (default) | all four on | Classification, at most one question at a time, an internal note with references |
| Classification and note | `clarify_enabled: false` | The requester is never written to; fields are set and the engineer gets a summary of what is there |
| Classification only | `clarify_enabled: false`, `handoff_note_enabled: false`, `similar_enabled: false` | Pure routing: service and subcategory are set, both logs stay empty |
| Clarification and note | `classify_enabled: false` | iTop's own rules classify the ticket; the module only completes it and summarizes |
| Note only | `classify_enabled: false`, `clarify_enabled: false` | A summary and similar-case references, nothing else touched |

Two combinations are rejected when you save them (422 from the admin API):

- `similar_enabled` without `handoff_note_enabled` — the references exist only inside the note, so there would be nothing to put them in;
- all of `classify_enabled`, `clarify_enabled` and `handoff_note_enabled` off — switching the module off entirely is `enabled: false`, which also stops it from being called at all.

With `handoff_note_enabled: false` the private log of the ticket stays empty whatever happens, `handoff_fallback_note` included: the run marks the ticket as processed and writes nothing. The run journal still records everything.

> [!IMPORTANT]
> `enabled` and `classes` are read at **startup**, not per ticket: changing them in the admin UI does not re-route webhooks until the service restarts. Every other setting applies from the next ticket.

Every run leaves a trace in [Admin UI → Runs](admin-ui.md#runs) (`GET /api/runs`): a `scope` step naming the actions the run was allowed to perform (`classify=on clarify=off …`), a `classification` step recording what the ticket arrived with, one `agent` step per model turn (the tools it called and with which arguments), one `tool:<name>` step per result (`[success]` / `[error]` plus the text), and a final `usage` step with model calls, tokens in/out and wall time.

---

## Selfcheck module settings

`selfcheck` is the platform's smoke module. It touches nothing: one run reads the service catalog over the configured iTop connection, asks the model to say hello, and records both in the run journal. Use it to confirm that a deployment's iTop and model connections work under real module code — on a timer, or on demand from [Admin UI → Modules](admin-ui.md#modules).

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable the module (a timer that calls a model is opt-in) |
| `interval_seconds` | `900` | How often the scheduled run fires |
| `probe_oql` | `SELECT Service` | The catalog read used as the iTop probe |
| `model` | _(global LLM model)_ | Override model for this module |

> [!IMPORTANT]
> `enabled` is read at **startup**, like intake's: the module registers its triggers or it does not. `interval_seconds` is re-read before every tick.

The scheduled run is journalled with `kind: schedule` and `subject: selfcheck` — a run's subject is whatever it is about, and only for ticket-scoped triggers is that a ticket reference.

---

## Supported LLM providers

`LLM_PROVIDER` (or the **Provider** dropdown in [Connections](admin-ui.md)) decides which of the other LLM fields matter. The UI hides the ones the provider does not use.

| `LLM_PROVIDER` | Base URL | API key | Covers |
|----------------|----------|---------|--------|
| `openai_compatible` (default) | required | optional | LM Studio (`http://localhost:1234/v1`), vLLM, LiteLLM Proxy (`http://litellm:4000/v1`), DeepSeek, Azure OpenAI (deployment name as model), OpenRouter, Together — anything serving `/chat/completions` |
| `openai` | not used | required (`sk-…`) | OpenAI directly |
| `google_genai` | not used | required | Google Gemini (native API, not the OpenAI shim) |
| `ollama` | optional, default `http://localhost:11434` | not used | Ollama's native API |

Adding a provider is a single entry in `assistant/src/itop_ai_assistant/core/llm_providers.py` — the setup API and the UI form are generated from that registry.

**Model parameters.** Anything the provider's client accepts goes in `LLM_PARAMS` (env, JSON) or the **Model parameters** field in Connections: `{"temperature": 0.2, "max_tokens": 2048, "timeout": 60}`. Connection fields (`model`, `base_url`, `api_key`) are rejected there — set them in their own fields.

**Reasoning models** (DeepSeek-R1, Qwen3, etc.) are supported out of the box — the assistant strips `<think>…</think>` blocks from responses before processing them. The stripped tag names are configurable in the LLM settings (`Think Tags` in the UI, or `LLM_THINK_TAGS` env var).

**Tool calling is a hard requirement.** The module runs as one agent loop, so it uses a single model (`model` in the Modules settings, or the global `LLM_MODEL`) and that model must call tools reliably — one that answers in prose instead of calling a tool wastes the run and closes the ticket with a fallback note. **Test LLM** checks this: it asks the model to call a probe tool and reports if it did not.

### Forced tool calls

The intake agent has no way to deliver plain text — the requester only sees what a tool posts — so where the endpoint allows it, the agent forbids prose outright by forcing a tool call. OpenAI and Gemini accept that; Ollama ignores it; DeepSeek rejects it with HTTP 400 ("Thinking mode does not support this tool_choice"). For the named providers the answer is built in and there is nothing to configure.

`openai_compatible` is the exception: the same setting fronts vLLM (accepts) and DeepSeek (rejects), so the deployment owner has to answer. Tick **Endpoint accepts a forced tool_choice** in Connections (or set `LLM_SUPPORTS_FORCED_TOOL_CHOICE=true`) and press **Test LLM** — the probe forces the choice and tells you whether the server took it. Leave it off if unsure: the agent then retries a prose answer once instead, which costs an extra model call but works everywhere.

---

## Vector index

Optional infrastructure: a semantic index of iTop objects in Qdrant. One thing reads it today: intake, for the references to similar solved tickets it puts in the handoff note (the `similar_*` settings [above](#intake-module-settings)). The rest of what is indexed waits for features still to come — KB matching, pattern analysis. Leave `QDRANT_URL` unset and the whole subsystem stays off: the assistant runs Redis-only exactly as before, and the handoff note simply carries no references.

Two independent **families** are indexed into their own collections today: **tickets** (`UserRequest`, `Incident` — the "similar past tickets" scenario) and **FAQ** (the "relevant knowledge base article" scenario, class `FAQ`). Each family gets its own Qdrant collection and its own HNSW graph — the two scenarios are never searched together in one call. A class belongs to a family through config (`vector.families.<family>.classes.<class>`), not through code, so a deployment can add a custom class to an existing family without a code change; a family may also set its own `sweep_interval_seconds`/`log_entries_per_chunk`, overriding the system-wide defaults below. Stock iTop's `FAQ` class has neither a lifecycle status nor a date attribute, so by default every article is indexed and every sweep pass re-reads the whole class (cheap: unchanged articles are neither re-embedded nor rewritten, only re-read) — map `status`/`last_update` in `faq_mapping` if your deployment's `FAQ` does carry either, or slow that family's own sweep interval down instead.

To switch it on:

1. Point `QDRANT_URL` at a Qdrant instance — the compose stack ships one (`http://qdrant:6333`). Collections are created lazily on first use; a failure is logged as a warning and never blocks the boot.
2. Configure the **Embeddings** connection (see [Admin UI → Connections](admin-ui.md#embeddings-tab)). The model must be **multilingual** — tickets are rarely single-language. `EMBEDDINGS_DIMENSION` must match what the model returns; **Test embeddings** measures the real dimension and tells you if it does not.
3. Turn indexing on in [Admin UI → Vector index](admin-ui.md#vector-index) and configure per-family, per-class settings there.

A background sweep picks up objects changed since the last pass, splits each one into the fragments configured for its class (`vector.families.<family>.classes.<class>.chunks` — see [Admin UI → Vector index](admin-ui.md#vector-index)), and embeds only what actually changed (content-hash guard). The index stores **embeddings, ids and filter metadata only — never ticket or article text**; anything shown to a user is re-fetched fresh from iTop, so the index is a rebuildable cache, not a copy of your ticket database.

FAQ fields (`title`, `summary`, `category_name`, `error_code`, `key_words`, `description`) map onto your iTop datamodel through the `faq_mapping` config section — same idea as `ticket_mapping` (see [Admin UI → Connections → Ticket mapping tab](admin-ui.md#ticket-mapping-tab)), but there is no dedicated admin UI tab for it yet: edit it through `GET`/`PATCH /api/setup/faq_mapping` (or `assistant/config.yaml`) until one exists. `status`, `org_id` and both dates are unmapped by default — stock iTop's `FAQ` carries none of them — map the ones your deployment's `FAQ` actually has.

Changing the embeddings model or its dimension invalidates every stored vector — vectors from different models are not comparable. The assistant refuses to mix them and asks for a full reindex instead (**Reindex** in the UI, or `uv run itop-ai-reindex --full` next to the deployment).
