# CLAUDE.md

This file provides guidance to Claude Code when working with code in this
repository.

## Project Overview

**itop-ai-assistant** — Python middleware that adds an AI layer on top of the
[Combodo iTop](https://www.itophub.io/) ITSM platform. iTop remains the system
of record; this service adds intelligence between users and engineers.

**Current MVP scope:** receive iTop webhooks, evaluate ticket completeness via
LLM, ask the user clarifying questions via iTop public log if needed, enrich
ticket fields before an engineer picks it up.

**Broader vision (next phases):**
- Pattern analysis across tickets (background jobs)
- Knowledge base maintenance automation
- AI-assisted Change Management review
- Engineer-facing contextual widget in iTop UI

## Architecture Principles

**iTop is the system of record.** Ticket content, conversation history, and
user data always come from iTop. Never cache or duplicate this data locally.
Read fresh on every webhook.

**Redis stores operational ticket state.** iTop is not the place for AI
metadata. Three fields per ticket — `rounds` (how many completeness clarifying
questions AI has asked), `classify_rounds` (how many classification clarifying
questions AI has asked), and `ai_done` (whether AI has finished processing) —
live in Redis with a configurable TTL (default 30 days). Redis also holds a
short-lived per-ticket processing lock (`lock:{ref}`) so concurrent webhooks
for the same ticket are not processed twice, plus the runtime overrides for
config, prompts and connection settings (`config:*`, `prompts:*`) and the
processing-run journal. This is the only state the service owns.

**AI acts as a named iTop user.** All comments posted to iTop are written on
behalf of a dedicated service account (e.g. `ai-assistant`). This makes AI
comments distinguishable from engineer and user comments without parsing text.

**Human-in-the-loop by default.** The AI acts autonomously only when confident
and the action is reversible. Asking a clarifying question and updating ticket
fields are autonomous. Resolving a ticket or reassigning it requires engineer
confirmation. When in doubt — do nothing, log the reason.

**One clarifying question at a time.** If the ticket description is incomplete,
post exactly one focused question to the public log. Max two rounds total —
after that, enrich with whatever is available and hand off to the engineer.

**Act only while the ticket is unassigned.** Before any action, check ticket
status. If an engineer has already picked it up (status changed from "New"),
stop processing silently. Check Redis `ai_done` first — if true, skip without
even calling iTop.

**Never react to our own comments.** Two lines of defense against webhook
loops: iTop trigger contexts must exclude `REST/JSON` (documented in README),
and the guard (`pipeline._stop_reason`) stops if the last public log entry was
posted by the AI service account — a misconfigured trigger degrades to a no-op
instead of an infinite question loop.

## iTop Domain Knowledge

See `.claude/rules/itop.md` for iTop-specific context: API patterns, ticket
lifecycle, object classes, webhook payload structure.

## Development Commands

All commands run from the `assistant/` directory unless noted.

**Install dependencies:**
```bash
uv sync          # all deps including dev
uv sync --no-dev # production only
```

**Run locally:**
```bash
uv run uvicorn itop_ai_assistant.main:app --host 0.0.0.0 --port 8001 --reload
```

**Run tests:**
```bash
uv run pytest                          # all tests
uv run pytest test/unit/test_router.py # single file
uv run pytest -k "test_name"           # single test by name
uv run pytest --cov=itop_ai_assistant  # with coverage
```

**Lint and format:**
```bash
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy src/       # type check
uv run pre-commit run --all-files
```

**Docker (full stack — iTop + assistant + Redis):**
```bash
cd docker && docker-compose up -d
```

**CI (`.github/workflows/ci.yml`)** runs on every push to `main` and every PR,
and gates the image publish (`docker-publish.yml` calls it via
`workflow_call`). It runs `ruff check`, `ruff format --check`,
`pre-commit run mypy --all-files` (the strict gate — *not* `uv run mypy src`),
`pytest --cov` and `pytest test/pg`, plus `npm run build` for the UI in a
parallel job. `test/integration` needs a real model endpoint and is excluded.
Before pushing, the same gates locally are `uv run pre-commit run --all-files`
and `uv run pytest`.

## Architecture

### Request Flow

1. iTop sends `POST /webhook` with `{id, class, event}` payload
2. Webhook handler returns HTTP 202 immediately; processing runs in background
   via `asyncio.create_task`
3. Fetch `TicketState` from Redis — if `ai_done: true`, stop immediately
4. Fetch full ticket from iTop API; for `UserRequest`/`Incident` also fetch
   related `Service` and `ServiceSubcategory`; fetch `Person` of caller
5. If ticket status is not "New" (engineer already working), stop silently
   (steps 3-5 are the guard — plain code in `pipeline._stop_reason`)
6. Run one tool-calling agent session with the run's tool set: an unclassified
   ticket gets the classification tools and the service catalog in the prompt,
   an already classified one gets neither
7. The agent decides, the tools enforce. `set_classification` writes Service +
   ServiceSubcategory (validating both IDs against the catalog);
   `post_public_question` posts one clarifying question and increments `rounds`
   or `classify_rounds`; `finish_handoff` writes the internal note and sets
   `ai_done`. A round budget spent means a fallback note instead of a question
8. The epilogue closes a run the agent did not close itself (prose-only
   answer, `max_iterations` burnt, loop): fallback note + `ai_done`, so the
   next webhook does not replay the whole cycle

### Key Source Files

| File (under `assistant/src/itop_ai_assistant/`) | Role                                                                                         |
|-------------------------------------------------|----------------------------------------------------------------------------------------------|
| `main.py`                                       | FastAPI app init, lifespan (builds `AppDeps`)                                                |
| `config.py`                                     | `Settings` — centralized config (pydantic-settings)                                          |
| `build_info.py`                                 | Build stamp baked at build time: version, commit, build date                                 |
| `deps.py`                                       | Composition root: `AppDeps`, `build_deps`, `create_llm`                                      |
| `config_store.py`                               | `RedisConfigStore` — runtime-editable module config                                          |
| `journal.py`                                    | `RunJournal` — per-run status/steps in Redis                                                 |
| `admin/router.py`                               | Admin API: config, prompts, runs, module discovery                                           |
| `admin/setup.py`                                | Setup API: connection sections + probes (wizard backend)                                     |
| `itop_provisioning.py`                          | iTop-side triggers/webhooks: find-or-create + CLI                                            |
| `webhook/router.py`                             | Webhook endpoint: auth, configured-gate, dispatch                                            |
| `pipelines/registry.py`                         | `PipelineRegistry` — (class, event) → module handler                                         |
| `text_utils.py`                                 | Generic `html_to_markdown`, `bind_oql`, `strip_thinking` (no biz deps)                       |
| `llm_providers.py`                              | Provider registry: connection shape + forced-`tool_choice` support                           |
| `prompt_store.py`                               | `PromptStore` — file-based templates with overrides                                          |
| `agents/intake/pipeline.py`                     | Intake module: registration, guard, agent run, epilogue, journal                             |
| `agents/intake/agent.py`                        | `create_agent` + tool-gate / terminal-exit / call-limit middleware                           |
| `agents/intake/tools.py`                        | Five tools, one invariant each; `ToolRejection`                                              |
| `agents/intake/prompt.py`                       | Initial messages: catalog, ticket, conversation as XML                                       |
| `agents/intake/prompts.py`                      | `IntakePrompts` + placeholder registry/validation                                            |
| `agents/intake/context.py`                      | `IntakeContext` — per-run dependencies for tools                                             |
| `prompts/intake/*.md`                           | Default intake prompts (system, catalog, ticket)                                             |
| `domain/ticket.py`                              | `Ticket` — semantic domain model (no iTop names)                                             |
| `ticket_repository.py`                          | `TicketRepository` — semantic ↔ iTop attribute adapter                                       |
| `catalog_repository.py`                         | `CatalogRepository` — service catalog reads                                                  |
| `domain/catalog.py`                             | `Service` / `ServiceSubcategory` semantic models                                             |
| `state/ticket_state.py`                         | Redis-backed `TicketState` and `TicketStateManager`                                          |
| `vector/db.py`                                  | `VectorDb` — lazy async Postgres engine + migrations runner                                  |
| `vector/models.py`                              | Vector store schema: static tables + `chunk_table` factory                                   |
| `vector/index.py`                               | `VectorIndex` — the single SQL/pgvector seam (versioned tables, KNN)                         |
| `vector/embedder.py`                            | `EmbeddingsClient` — OpenAI-compatible /v1/embeddings, batching                              |
| `vector/chunker.py`                             | Pure chunking: profiles → chunks, token budget, log windows                                  |
| `vector/source.py`                              | `VectorSource` protocol + `VectorRecord` — the indexer's only contract with a content source |
| `vector/indexer.py`                             | `VectorIndexer` — background sweep, backfill, reconciliation                                 |
| `vector/reindex.py`                             | Backfill/reindex CLI (`python -m vector.reindex`)                                            |
| `vector/router.py`                              | `GET /api/vector/status`, `POST /api/vector/reindex`                                         |
| `vector/migrations/`                            | Alembic migrations (applied automatically at startup)                                        |
| `vector_sources/registry.py`                    | `build_vector_sources()` — one line per content source                                       |
| `vector_sources/tickets.py`                     | `TicketVectorSource` — the only source today (tickets)                                       |
| `itop_client/`                                  | `Itop` — vendored iTop REST API library (itoptop fork)                                       |

**`src/itop_ai_assistant/itop_client/` is a vendored external library** (fork of itoptop,
rewritten with httpx). Keep it self-contained and generic: no imports from
this application, and do not remove functionality that this service happens
not to use. Application-specific logic belongs in `ticket_repository.py`.

**Dependency injection:** no module-level singletons. `build_deps()` in
`src/itop_ai_assistant/deps.py` assembles all shared dependencies at startup (FastAPI lifespan,
stored in `app.state.deps`). Each processing run builds an `IntakeContext` with
a config snapshot from `ConfigStore` and a per-run LLM client — tools take
everything from `runtime.context`, never from globals or `get_settings()`.
The iTop client and repositories come from `ItopProvider` (`deps.itop.get()`
→ `ItopBundle`): the bundle is cached by a fingerprint of the `itop` +
`ticket_mapping` sections and rebuilt (old client closed, repo caches
dropped) when the runtime config changes — connection edits apply from the
next ticket without a restart.

**Pipeline registry:** webhook events reach business modules through
`PipelineRegistry` — a startup-built map of `(object class, event)` → handler.
The router accepts only registered combinations. Adding a new module: create
`src/itop_ai_assistant/agents/<module>/pipeline.py` with `register(registry, settings)` exposing
a `ModuleInfo` (name, description, config model, prompt names — consumed by
the admin UI) and its routes, add one call in
`pipelines/registry.py::build_registry`, add a config section in `config.py`.
`ModuleInfo.validate_prompts` is called for every registered module at startup,
so a broken template fails the boot instead of a live ticket. The intake module
is enabled/scoped via `intake.enabled` (default `true`) and `intake.classes`
(default `[UserRequest, Incident]`).

**`src/itop_ai_assistant/agents/intake/` — the ticket-processing module.** Classify
Service/ServiceSubcategory, ask one clarifying question, post the handoff
note, set `ai_done` — as a single tool-calling agent
(`langchain.agents.create_agent`). Deterministic shell, agentic core: the
per-ticket lock, the guard (`_stop_reason` — three checks) and the epilogue are
plain code in `pipeline.py`; everything between them is the agent's decision.
Five tools (`tools.py`), each enforcing one invariant and rejecting bad calls
with a `ToolRejection` that says what to do instead; the round counters are
picked by code, never by the model. The tool set is **per run**, not fixed:
`tools_for(ticket)` withholds the three classification tools once the ticket
has both a service and a subcategory, and `build_initial_messages` then omits
the catalog — the agent otherwise re-classifies on every webhook, and once
proposed a different subcategory over a correct one. Enforcing the rule by
taking the tools away beats asking for it in the prompt. Four
middleware: `_tool_gate` turns
`ToolRejection` into an error `ToolMessage` (real failures propagate and fail
the run), `_stop_after_terminal` ends the run once `post_public_question` or
`finish_handoff` succeeded, `_require_tool_call` retries a prose-only turn
once (observed in production: with a conversation in the prompt the model
continues the *dialogue* instead of calling the tool, and the text reaches
nobody), and `_force_tool_choice` makes prose impossible instead of merely
correctable — added only when the endpoint accepts `tool_choice="any"`
(`LlmConfig.endpoint_forces_tool_choice`, answered by `llm_providers` or by
the deployment owner for `openai_compatible`). **Which endpoint accepts what
is a fact about the connection (`llm_providers`); whether to use it is the
agent's call** — intake forces it everywhere it can because its plain text
reaches nobody, while an agent that must answer in prose simply passes
`force_tool_choice=False` (the default). `_require_tool_call` stays on
regardless: Ollama and some gateways drop the field silently rather than
erroring. Note that returning `Command(goto="__end__")`
from `wrap_tool_call` does *not* end the run — the conditional edge
`create_agent` puts on the tools node fires anyway.

Every run leaves a full trace in `/api/runs`: every model turn, every tool
result and a final `usage` step (model calls, tokens, wall time).

The module lives under `src/itop_ai_assistant/agents/` rather than `src/itop_ai_assistant/graph/` because its flow
is not an explicit graph — keep that convention for future modules: `agents/`
for "the model decides the order", `graph/` for "the code does". It won an A/B
against a deterministic LangGraph module (`src/itop_ai_assistant/graph/enrichment/`, five nodes
doing the same job) which was deleted whole once intake proved itself; if you
need the comparison, `git log --diff-filter=D -- assistant/src/graph` has it.

**Domain model, not raw dicts:** processing code works with the semantic
`Ticket` model (`domain/ticket.py`) — fields like `subcategory_id`,
`caller_name`, `ticket.label`, `ticket.has_service`. Translation to actual
iTop attribute names happens only in `TicketRepository`, driven by the
`ticket_mapping` config: `fields` (semantic → attribute code),
`class_overrides` (per-class differences, e.g. `Incident` has no
`request_type`), `active_statuses` (when the assistant may act). Adapting to
a customized iTop datamodel is a config change, not a code change. Service
catalog reads go through `CatalogRepository` (fixed `Service`/
`ServiceSubcategory` classes — those are practically never customized),
tools see the `Service`/`ServiceSubcategory` models only (distinct iTop
classes get distinct models). Tools never touch the raw iTop client or
attribute names — all iTop access goes through the repositories; OQL
templates use semantic `:this->field` placeholders bound from
`ticket.model_dump()`.

### LLM Stack

**`init_chat_model`** (langchain) builds the client, one provider per entry in
`src/itop_ai_assistant/llm_providers.py` — `openai_compatible` (default, any `/chat/completions`
URL: LM Studio, vLLM, LiteLLM, DeepSeek, Azure, OpenRouter), `openai`,
`google_genai`, `ollama`. `create_llm` (`deps.py`) is the only construction
site and returns `BaseChatModel`; every consumer types it that way, so adding
a provider is a registry entry plus its `langchain-*` package, never a change
in the agent. The registry also records which connection fields matter
(`base_url_mode` / `api_key_mode` — the setup API and the UI form are
generated from it) and whether the endpoint accepts a forced `tool_choice`.
`llm.params` is forwarded verbatim to the client (temperature, max_tokens, …);
connection fields are rejected there. Plain text responses, no structured
output. `strip_thinking` removes `<think>…</think>` blocks emitted by
reasoning models (DeepSeek-R1, Qwen3, etc.).

**langchain** (v1 `create_agent` API) is the agent framework — tool-calling
agents, `@tool` + `ToolRuntime`, `wrap_tool_call` / `before_model` middleware.
Avoid plain LangChain chains for anything beyond a single LLM call.

**langgraph** is a required dependency of `langchain` (`create_agent` is built
on it) and is imported directly in one place only, for the
`CompiledStateGraph` return type in `agents/intake/agent.py`. Reach for it
explicitly if a future module needs a genuinely deterministic multi-step flow —
that is what `src/itop_ai_assistant/graph/` is reserved for.

### Configuration

Config is centralized in `src/itop_ai_assistant/config.py` using **pydantic-settings**.
Priority (high → low): Redis runtime overrides (setup/admin API) → env vars
→ `.env` file → `config.yaml` → field defaults.

`config.yaml` (committed to repo) holds non-secret defaults. Secrets and
environment-specific values go in `.env` (not committed) or are set at
runtime through the setup API.

**No field is required at startup.** The app always boots; until the `itop`
and `llm` sections are complete (`missing_setup()` in `config.py`), `/webhook`
returns 503 and the admin API stays available for the setup wizard.

**Runtime-editable sections** (`ItopConfig`, `LlmConfig`, `SecurityConfig`,
`TicketMappingConfig`) are served by `RedisConfigStore` under `config:{name}`;
env fields act as their defaults via `Settings.itop` / `.llm` / `.security`
properties. Secrets inside sections are plain `str` (storage round-trip);
masking lives in the setup API (`SECRET_FIELDS`). Blank strings normalize to
None (`RuntimeSectionConfig`). Webhook/admin token checks read the effective
`security` section per request.

| Field (env) | Required | Purpose |
|-------|----------|---------|
| `itop_url` | required (env or setup API) | iTop REST API base URL (no default) |
| `itop_api_version` | default `1.3` | iTop REST API version |
| `itop_timeout` | default `30.0` | HTTP timeout (seconds) for iTop requests |
| `itop_user` + `itop_pwd` | one of (env or setup API) | iTop basic auth |
| `itop_token` | one of (env or setup API) | iTop token auth (alternative to user+pwd) |
| `webhook_token` | recommended | Shared secret for `/webhook` (`X-Auth-Token` header); unset = no auth |
| `admin_token` | recommended | Bearer token for `/api` admin endpoints (`Authorization: Bearer`); unset = no auth (first-run mode) |
| `prompts_dir` | optional (env-only) | Directory with per-deployment prompt overrides |
| `ui_dist_dir` | optional (env-only) | Where the built admin SPA lives; the image sets `/app/ui/dist`, a source checkout is auto-detected |
| `llm_provider` | default `openai_compatible` | Which entry of `llm_providers.PROVIDERS` to build the client from |
| `llm_base_url` | required by the provider's `base_url_mode` | Endpoint URL; unused by `openai` / `google_genai` |
| `llm_model` | required (env or setup API) | Model name as exposed by the endpoint |
| `llm_api_key` | required by the provider's `api_key_mode` | API key (local endpoints ignore it) |
| `llm_params` | optional | JSON of extra client kwargs (temperature, max_tokens, …) |
| `llm_supports_forced_tool_choice` | optional | Endpoint accepts `tool_choice="any"`; `None` = ask the registry. Only meaningful for `openai_compatible` |
| `llm_think_tags` | default `[think, thinking, reasoning]` | Tag names stripped as inline reasoning blocks |
| `redis_url` | default (env-only, bootstrap) | Redis connection URL |
| `database_url` | optional (env-only, bootstrap) | Postgres DSN for the vector store (`postgresql+asyncpg://…`); unset = Redis-only deployment |
| `embeddings_base_url` | optional (env or setup API) | OpenAI-compatible /v1 endpoint for embeddings |
| `embeddings_model` | optional (env or setup API) | Embedding model (must be multilingual, e.g. `bge-m3`) |
| `embeddings_api_key` | optional | API key for the embeddings endpoint |
| `embeddings_dimension` | default `1024` (max 4000) | Vector dimension; must match the model — verified by `test-embeddings` |
| `embeddings_batch_size` | default `32` | Texts per /embeddings request |
| `state_ttl_days` | default `30` | TTL for per-ticket state in Redis |
| `run_ttl_days` | default `7` | TTL for processing-run journal entries |
| `log_level` | default `INFO` (env-only) | Logging level |

Per-module limits live in `IntakeConfig` (`intake.*`): `max_rounds` and
`max_classify_rounds` (both default 2) cap clarifying-question rounds,
`max_iterations` (default 8) caps model calls per run, and `model` optionally
overrides the global `llm_model` for the module (set via `config.yaml`, e.g.
`intake: model: ...`). There is one model for the whole module — the agent is a
single loop — and it must be a reliable tool-caller. `enabled` and `classes`
are read at startup (`build_registry` takes `Settings`, not `ConfigStore`) —
editing them in the admin UI does not re-route webhooks until a restart; every
other field is read per run.

**Runtime-editable config and prompts.** Business config (module sections
like `intake.*`) and prompts can be edited at runtime through the
admin API (`/api/config/...`, `/api/prompts/...`): overrides live in Redis
on top of env/yaml/file defaults and apply from the next processed ticket.
Reads degrade to defaults when Redis is unavailable; writes are validated
(pydantic for config, placeholder registry for prompts) before storing.
Every processing run leaves a trace in the `RunJournal` (status, steps,
error) — journal writes are non-fatal by design. Inspect via
`GET /api/runs`.

**Setup API (wizard backend).** Connection sections are managed via
`/api/setup`: `GET /status` (what's missing), `GET/PATCH/DELETE /{section}`
for `itop` / `llm` / `security` / `ticket_mapping` / `embeddings` / `vector`,
`GET /llm-providers` (the `llm_providers` registry as JSON — the UI builds
the connection form from it instead of duplicating the list in TypeScript),
`POST /test-itop`, `POST /test-llm` and `POST /test-embeddings` probes
(nothing saved; `test-llm` also binds a probe tool and calls it, forcing
`tool_choice` when the section says the endpoint accepts it, so a
non-tool-calling model or a rejected forcing shows up in the wizard instead
of on the first live ticket; `test-embeddings` measures the endpoint's real
vector dimension and reports `dimension_match`). PATCH is a partial update merged
over the current effective config; GET responses mask secrets
(`secrets: {field: is_set}`); in PATCH bodies an absent field keeps the
stored value, explicit `null` clears it. Until an admin token is set the
admin API is open (first-run mode). Redis persistence is required for
runtime config to survive restarts (compose already enables appendonly).
`POST /provision-itop` creates the iTop-side triggers and webhooks
(`itop_provisioning.py`, find-or-create by name, webhook auth via
`X-Auth-Token`) under one-time admin credentials from the body — never
stored; the same logic runs standalone as a CLI
(`uv run itop-ai-provision`). The wizard step
order is Security → iTop → iTop webhooks → LLM: provisioning needs the
saved webhook token, so security comes first and the LLM step last.

**Vector store (optional infrastructure, `src/itop_ai_assistant/vector/`).** Postgres +
pgvector behind the env-only `database_url`; unset = the whole subsystem is
off and the deployment stays Redis-only. `src/itop_ai_assistant/vector/` is an infrastructure
layer like `state/` or `journal.py` — it is NOT a business module: it does
not register in `PipelineRegistry`, has no prompts or webhook routes; future
business modules consume it through `AppDeps.vector_db`. Alembic migrations
(static tables: `vector_index_meta`, `vector_sync_state`, `index_journal`)
run automatically at startup when `database_url` is set — failures degrade
to a warning, never a boot failure. The versioned chunk tables
(`vector_chunk_v{N}`, dimension from the `embeddings` section) are created
at runtime by `VectorIndex.ensure_version()`; a model/dimension change
raises `FingerprintMismatchError` instead of mixing incomparable vectors.
Diagnostics: `GET /api/vector/status`. The chunk tables store embeddings +
ids + filter metadata only — never raw ticket text (see
`docs/plans/vector-store.md`).

The index is filled by `VectorIndexer` (`src/itop_ai_assistant/vector/indexer.py`) — the
project's first background task, started in the lifespan when `database_url`
is set (`app.state.vector_indexer`, stopped before `deps.aclose()`). Every
`vector.sweep_interval_seconds` it re-reads the runtime config (so flipping
`vector.enabled` needs no restart), takes a Postgres advisory lock (safe with
replicas) and sweeps: reads objects changed since the per-class cursor
(`last_update`, 2×interval overlap, paged with `sweep_throttle_seconds`
between pages), chunks them per `vector.classes[<class>].profile` (chunk
kinds = profile keys; log kinds `log:public`/`log:private` are implemented
but not in the default profiles), embeds only changed chunks (sha256
hash-guard) and deletes vanished ones; objects whose relevance value is
outside the per-class `index_values` get their chunks removed (empty list =
index everything).

**The indexer knows nothing about iTop or tickets.** It drives the
`VectorSource` protocol (`vector/source.py`): a source yields `VectorRecord`s
(identity, a last-modification datetime, a relevance value, source-defined
`filters`, and an opaque `payload`) and chunks them back on request. Which
iTop attributes those map to is the source's own concern — `TicketVectorSource`
(`vector_sources/tickets.py`) is the only implementation today and wraps
`TicketRepository` + `CatalogRepository` (semantic `status`/`last_update` via
`ticket_mapping`). Adding a content source (KB articles, KnownErrors, …) means
a new `src/itop_ai_assistant/vector_sources/<name>.py` plus one line in
`vector_sources/registry.py` — same one-function-to-extend pattern as
`pipelines/registry.py`, and no change to `vector/`. A configured class with no
registered source logs a warning and is skipped.
The cursor advances once per completed class pass (iTop OQL has no ORDER BY).
Every `vector.reconcile_interval_days` a reconciliation pass deletes chunks
of objects that disappeared from iTop. Runs are journaled in the
`index_journal` table (visible in `/api/vector/status`). Full rebuild:
`POST /api/vector/reindex` (resets cursors, wakes the sweep) or the CLI
`uv run itop-ai-reindex --full` (reads runtime config
from Redis, so run it next to the deployment).

**Prompts are files, not code.** Defaults ship inside the package
(`src/itop_ai_assistant/prompts/<module>/*.md` — currently `intake/` — exposed
as `prompt_store.PACKAGED_PROMPTS_DIR`); a deployment overrides individual prompts by
placing same-named files under `<prompts_dir>/<module>/`. Placeholders are
validated against `PROMPT_VARIABLES` (in the module's `prompts.py`) at
startup — adding a new placeholder to a prompt requires adding it there and
passing the value where the messages are built (`prompt.py`). Prompt files are re-read on
every run, so edits apply without restart. Exception: **intake tool
docstrings are code**, not prompts — they must stay in sync with the
signatures and with the invariants enforced inside the tools.

See `docker/.env.dist` for a full template.

## Admin UI (`ui/`)

The admin SPA (setup wizard, settings, prompts, run monitoring) lives in
`ui/` and is built with **Vite + React + TypeScript + Mantine**. It is
maintained primarily with AI assistance by a non-frontend developer, so
simplicity beats elegance. These constraints are mandatory:

- **Minimal dependencies**: `react`, `react-dom`, `react-router-dom`,
  `@mantine/core`, `@mantine/form` (plus their peer deps) — nothing else.
  No Redux, TanStack Query, axios, or CSS-in-JS libraries: state is
  `useState`, HTTP is the single fetch wrapper in `api.ts`.
- **Flat structure**: one file per screen (`SetupWizard.tsx`,
  `Connections.tsx`, `Modules.tsx`, `Prompts.tsx`, `Runs.tsx`,
  `Vector.tsx`) plus `api.ts` and `Layout.tsx`. No hook factories,
  barrel files, or clever abstractions.
- **Pin exact versions** in `package.json` (no `^`/`~`), commit the lock
  file; upgrade dependencies only when something requires it.
- **Prompt editor is a plain Mantine `Textarea`** — introduce CodeMirror
  only if syntax highlighting becomes a real need.
- The SPA builds into `ui/dist`; FastAPI serves it via `StaticFiles` at
  `/ui` (API stays under `/api`). In dev, use the vite proxy to `:8001` —
  no CORS. The admin token lives in `localStorage`; 401 shows the token
  entry screen.

**Commands** (run from `ui/`; the dev server proxies `/api`, `/health` and `/version`
to the assistant on `:8001`, so run the backend alongside):

```bash
npm ci          # install pinned dependencies
npm run dev     # vite dev server with hot reload
npm run build   # type-check (tsc --noEmit) + production build into ui/dist
```

## Testing Notes

- Tests live in `assistant/test/unit/`
- `pytest.toml` sets `importmode = importlib`; the package itself is on the
  path because `uv sync` installs it into the venv (editable)
- LLM calls and HTTP requests are mocked — no real iTop or LLM needed
- Redis is mocked with `fakeredis`
- `get_settings()` is cached via `lru_cache`; call `get_settings.cache_clear()`
  in `setUp`/`tearDown` when tests need to control env vars
- Postgres/pgvector integration tests live in `assistant/test/pg/` — NOT
  collected by default; run explicitly with `uv run pytest test/pg` (needs
  Docker: Testcontainers spins up `pgvector/pgvector:pg17`, skips when
  Docker is unavailable)
- `assistant/test/integration/` holds the only tests that call a **real LLM**
  (iTop is still mocked via `ItopMockTransport`, Redis via `fakeredis`). Also
  not collected by default (`testpaths = ["test/unit"]`); needs `.env.test`
  (see `.env.test.dist`) and a reachable endpoint. This is where prompt and
  tool-calling regressions show up — run `uv run pytest test/integration`
  after touching `prompts/intake/*.md` or a tool signature.
- Current test files: `test_config.py`, `test_router.py`, `test_deps.py`,
  `test_pipelines_registry.py`, `test_ticket_state.py`, `test_prompt_store.py`,
  `test_ticket_repository.py`, `test_catalog_repository.py`,
  `test_itop_schema.py`, `test_itop_provisioning.py`, `test_journal.py`,
  `test_config_store.py`, `test_admin_api.py`, `test_setup_api.py`,
  `test_text_utils.py`, `test_llm_providers.py`, `test_embedder.py`, `test_vector_status.py`,
  `test_chunker.py`, `test_indexer.py`, `test_vector_sources_tickets.py`,
  `test_intake_pipeline.py`, `test_intake_prompt.py`, `test_intake_tools.py`,
  `test_intake_agent.py`; in `test/pg/`: `test_db_smoke.py`,
  `test_vector_index.py`, `test_indexer_pg.py`; in `test/integration/`:
  `test_intake_agent_live.py`
- The intake agent loop is tested without an LLM through a scripted
  `FakeToolCallingModel(BaseChatModel)` (`test_intake_agent.py`) —
  `create_agent` calls `bind_tools`, which `BaseChatModel` leaves
  unimplemented, so the ready-made langchain-core fakes do not fit. Tools are
  called directly as `tools.<name>.coroutine(...)`, bypassing pydantic.
