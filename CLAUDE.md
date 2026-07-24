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
and the guard node stops if the last public log entry was posted by the AI
service account — a misconfigured trigger degrades to a no-op instead of an
infinite question loop.

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
uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload
```

**Run tests:**
```bash
uv run pytest                          # all tests
uv run pytest test/unit/test_router.py # single file
uv run pytest -k "test_name"           # single test by name
uv run pytest --cov=src                # with coverage
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

## Architecture

### Request Flow

1. iTop sends `POST /webhook` with `{id, class, event}` payload
2. Webhook handler returns HTTP 202 immediately; processing runs in background
   via `asyncio.create_task`
3. Fetch `TicketState` from Redis — if `ai_done: true`, stop immediately
4. Fetch full ticket from iTop API; for `UserRequest`/`Incident` also fetch
   related `Service` and `ServiceSubcategory`; fetch `Person` of caller
5. If ticket status is not "New" (engineer already working), stop silently
6. **Classify:** if Service/ServiceSubcategory are not set, LLM queries iTop
   for available options and selects the best match; if it cannot determine
   confidently, posts one clarifying question, increments `classify_rounds`
7. LLM evaluates whether ticket description is sufficient for the engineer
8. **If incomplete:** post one clarifying question as a public log entry via
   iTop API, increment `rounds` in Redis
9. **If complete (or rounds exhausted):** post structured internal note for 
   the engineer, set `ai_done: true` in Redis

### Key Source Files

| File                                            | Role                                                |
|-------------------------------------------------|-----------------------------------------------------|
| `src/main.py`                                   | FastAPI app init, lifespan (builds `AppDeps`)       |
| `src/config.py`                                 | `Settings` — centralized config (pydantic-settings) |
| `src/deps.py`                                   | Composition root: `AppDeps`, `build_deps`, `create_llm` |
| `src/config_store.py`                           | `RedisConfigStore` — runtime-editable module config |
| `src/journal.py`                                | `RunJournal` — per-run status/steps in Redis        |
| `src/admin/router.py`                           | Admin API: config, prompts, runs, module discovery  |
| `src/admin/setup.py`                            | Setup API: connection sections + probes (wizard backend) |
| `src/itop_provisioning.py`                      | iTop-side triggers/webhooks: find-or-create + CLI   |
| `src/webhook/router.py`                         | Webhook endpoint: auth, configured-gate, dispatch   |
| `src/pipelines/registry.py`                     | `PipelineRegistry` — (class, event) → module handler |
| `src/graph/enrichment/pipeline.py`              | Enrichment module: registration + event handlers    |
| `src/graph/enrichment/graph.py`                 | LangGraph graph definition and compilation          |
| `src/graph/enrichment/nodes/guard.py`           | Pre-check node (ai_done, status, last-entry-not-AI) |
| `src/graph/enrichment/nodes/classify.py`        | LLM classification node (Service/ServiceSubcategory)|
| `src/graph/enrichment/nodes/evaluate.py`        | LLM completeness evaluation node                   |
| `src/graph/enrichment/nodes/ask.py`             | Post clarifying question node                       |
| `src/graph/enrichment/nodes/enrich.py`          | Ticket enrichment node                              |
| `src/graph/enrichment/nodes/utils.py`           | `strip_thinking` + re-exports from `text_utils`     |
| `src/text_utils.py`                             | Generic `html_to_markdown`, `bind_oql` (no biz deps)|
| `src/graph/enrichment/prompts.py`               | `EnrichmentPrompts` + placeholder registry/validation |
| `src/prompt_store.py`                           | `PromptStore` — file-based templates with overrides |
| `prompts/enrichment/*.md`                       | Default prompt templates (one file per prompt)      |
| `src/graph/enrichment/context.py`               | `GraphContext` — per-run dependencies for nodes     |
| `src/domain/ticket.py`                          | `Ticket` — semantic domain model (no iTop names)    |
| `src/ticket_repository.py`                      | `TicketRepository` — semantic ↔ iTop attribute adapter |
| `src/catalog_repository.py`                     | `CatalogRepository` — service catalog reads         |
| `src/domain/catalog.py`                         | `Service` / `ServiceSubcategory` semantic models    |
| `src/state/ticket_state.py`                     | Redis-backed `TicketState` and `TicketStateManager` |
| `src/vector/db.py`                              | `VectorDb` — lazy async Postgres engine + migrations runner |
| `src/vector/models.py`                          | Vector store schema: static tables + `chunk_table` factory |
| `src/vector/index.py`                           | `VectorIndex` — the single SQL/pgvector seam (versioned tables, KNN) |
| `src/vector/embedder.py`                        | `EmbeddingsClient` — OpenAI-compatible /v1/embeddings, batching |
| `src/vector/chunker.py`                         | Pure chunking: profiles → chunks, token budget, log windows |
| `src/vector/indexer.py`                         | `VectorIndexer` — background sweep, backfill, reconciliation |
| `src/vector/reindex.py`                         | Backfill/reindex CLI (`python -m vector.reindex`)   |
| `src/vector/router.py`                          | `GET /api/vector/status`, `POST /api/vector/reindex` |
| `src/vector/migrations/`                        | Alembic migrations (applied automatically at startup) |
| `src/itop_client/`                              | `Itop` — vendored iTop REST API library (itoptop fork) |

**`src/itop_client/` is a vendored external library** (fork of itoptop,
rewritten with httpx). Keep it self-contained and generic: no imports from
this application, and do not remove functionality that this service happens
not to use. Application-specific logic belongs in `ticket_repository.py`.

**Dependency injection:** no module-level singletons. `build_deps()` in
`src/deps.py` assembles all shared dependencies at startup (FastAPI lifespan,
stored in `app.state.deps`). Each processing run builds a `GraphContext` with
a config snapshot from `ConfigStore` and per-run LLM clients — nodes take
everything from `runtime.context`, never from globals or `get_settings()`.
The iTop client and repositories come from `ItopProvider` (`deps.itop.get()`
→ `ItopBundle`): the bundle is cached by a fingerprint of the `itop` +
`ticket_mapping` sections and rebuilt (old client closed, repo caches
dropped) when the runtime config changes — connection edits apply from the
next ticket without a restart.

**Pipeline registry:** webhook events reach business modules through
`PipelineRegistry` — a startup-built map of `(object class, event)` → handler.
The router accepts only registered combinations. Adding a new module: create
`src/graph/<module>/pipeline.py` with `register(registry, settings)` exposing
a `ModuleInfo` (name, description, config model, prompt names — consumed by
the future admin UI) and its routes, add one call in
`pipelines/registry.py::build_registry`, add a config section in `config.py`.
The enrichment module is enabled/scoped via `enrichment.enabled` and
`enrichment.classes` (default `[UserRequest, Incident]`).

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
nodes see the `Service`/`ServiceSubcategory` models only (distinct iTop
classes get distinct models). Nodes never touch the raw iTop client or
attribute names — all iTop access goes through the repositories; OQL
templates use semantic `:this->field` placeholders bound from
`ticket.model_dump()`.

### LLM Stack

**langchain-openai** (`ChatOpenAI`) as the LLM client — routes to any
OpenAI-compatible endpoint via `LLM_BASE_URL` (LM Studio locally, LiteLLM
Proxy or direct cloud API in production). Plain text responses, no structured
output. `strip_thinking` removes `<think>…</think>` blocks emitted by
reasoning models (DeepSeek-R1, Qwen3, etc.).

**LangGraph** for all agent logic with branching or multi-step flow. Avoid
plain LangChain chains for anything beyond a single LLM call.

### Configuration

Config is centralized in `src/config.py` using **pydantic-settings**.
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
| `llm_base_url` | required (env or setup API) | OpenAI-compatible endpoint (no default) |
| `llm_model` | required (env or setup API) | Model name as exposed by the endpoint |
| `llm_api_key` | optional | API key (omit for local LM Studio) |
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

Per-module limits live in `EnrichmentConfig` (`enrichment.*`): `max_rounds`
and `max_classify_rounds` (both default 2) cap clarifying-question rounds;
`classify_model` / `evaluate_model` / `enrich_model` optionally override the
global `llm_model` per node (set via `config.yaml`, e.g. `enrichment:
classify_model: ...`).

**Runtime-editable config and prompts.** Business config (module sections
like `enrichment.*`) and prompts can be edited at runtime through the
admin API (`/api/config/...`, `/api/prompts/...`): overrides live in Redis
on top of env/yaml/file defaults and apply from the next processed ticket.
Reads degrade to defaults when Redis is unavailable; writes are validated
(pydantic for config, placeholder registry for prompts) before storing.
Every processing run leaves a trace in the `RunJournal` (status, node
steps, error) — journal writes are non-fatal by design. Inspect via
`GET /api/runs`.

**Setup API (wizard backend).** Connection sections are managed via
`/api/setup`: `GET /status` (what's missing), `GET/PATCH/DELETE /{section}`
for `itop` / `llm` / `security` / `ticket_mapping` / `embeddings` / `vector`,
`POST /test-itop`, `POST /test-llm` and `POST /test-embeddings` probes
(nothing saved; `test-embeddings` measures the endpoint's real vector
dimension and reports `dimension_match`). PATCH is a partial update merged
over the current effective config; GET responses mask secrets
(`secrets: {field: is_set}`); in PATCH bodies an absent field keeps the
stored value, explicit `null` clears it. Until an admin token is set the
admin API is open (first-run mode). Redis persistence is required for
runtime config to survive restarts (compose already enables appendonly).
`POST /provision-itop` creates the iTop-side triggers and webhooks
(`itop_provisioning.py`, find-or-create by name, webhook auth via
`X-Auth-Token`) under one-time admin credentials from the body — never
stored; the same logic runs standalone as a CLI
(`PYTHONPATH=src uv run python -m itop_provisioning`). The wizard step
order is Security → iTop → LLM → iTop webhooks: provisioning needs the
saved webhook token.

**Vector store (optional infrastructure, `src/vector/`).** Postgres +
pgvector behind the env-only `database_url`; unset = the whole subsystem is
off and the deployment stays Redis-only. `src/vector/` is an infrastructure
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

The index is filled by `VectorIndexer` (`src/vector/indexer.py`) — the
project's first background task, started in the lifespan when `database_url`
is set (`app.state.vector_indexer`, stopped before `deps.aclose()`). Every
`vector.sweep_interval_seconds` it re-reads the runtime config (so flipping
`vector.enabled` needs no restart), takes a Postgres advisory lock (safe with
replicas) and sweeps: reads tickets changed since the per-class cursor
(`last_update`, 2×interval overlap, paged with `sweep_throttle_seconds`
between pages), chunks them per `vector.classes[<class>].profile` (chunk
kinds = profile keys; log kinds `log:public`/`log:private` are implemented
but not in the default profiles), embeds only changed chunks (sha256
hash-guard) and deletes vanished ones; objects whose relevance value is
outside the per-class `index_values` get their chunks removed (empty list =
index everything). The source contract (`vector/source.py`): every indexed
class exposes a last-modification datetime and a relevance attribute — which
attributes those are is the source's mapping concern (tickets: semantic
`status`/`last_update` via `ticket_mapping`).
The cursor advances once per completed class pass (iTop OQL has no ORDER BY).
Every `vector.reconcile_interval_days` a reconciliation pass deletes chunks
of objects that disappeared from iTop. Runs are journaled in the
`index_journal` table (visible in `/api/vector/status`). Full rebuild:
`POST /api/vector/reindex` (resets cursors, wakes the sweep) or the CLI
`PYTHONPATH=src uv run python -m vector.reindex --full` (reads runtime config
from Redis, so run it next to the deployment).

**Prompts are files, not code.** Defaults live in `prompts/enrichment/*.md`;
a deployment overrides individual prompts by placing same-named files under
`<prompts_dir>/enrichment/`. Placeholders are validated against
`PROMPT_VARIABLES` (in `graph/enrichment/prompts.py`) at startup — adding a
new placeholder to a prompt requires adding it there and passing the value at
invoke time in the node. Prompt files are re-read on every run, so edits apply
without restart.

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

**Commands** (run from `ui/`; the dev server proxies `/api` and `/health`
to the assistant on `:8001`, so run the backend alongside):

```bash
npm ci          # install pinned dependencies
npm run dev     # vite dev server with hot reload
npm run build   # type-check (tsc --noEmit) + production build into ui/dist
```

## Testing Notes

- Tests live in `assistant/test/unit/`
- `pytest.toml` sets `pythonpath = ["src"]` and `importmode = importlib`
- LLM calls and HTTP requests are mocked — no real iTop or LLM needed
- Redis is mocked with `fakeredis`
- `get_settings()` is cached via `lru_cache`; call `get_settings.cache_clear()`
  in `setUp`/`tearDown` when tests need to control env vars
- Postgres/pgvector integration tests live in `assistant/test/pg/` — NOT
  collected by default; run explicitly with `uv run pytest test/pg` (needs
  Docker: Testcontainers spins up `pgvector/pgvector:pg17`, skips when
  Docker is unavailable)
- Current test files: `test_config.py`, `test_router.py`, `test_deps.py`,
  `test_enrichment_pipeline.py`, `test_pipelines_registry.py`,
  `test_ticket_state.py`, `test_prompt_store.py`, `test_ticket_repository.py`,
  `test_catalog_repository.py`, `test_itop_schema.py`, `test_journal.py`,
  `test_config_store.py`, `test_admin_api.py`, `test_setup_api.py`,
  `test_nodes_guard.py`, `test_nodes_classify.py`, `test_nodes_evaluate.py`,
  `test_nodes_ask.py`, `test_nodes_enrich.py`, `test_nodes_utils.py`,
  `test_embedder.py`, `test_vector_status.py`, `test_chunker.py`,
  `test_indexer.py`; in `test/pg/`: `test_db_smoke.py`,
  `test_vector_index.py`, `test_indexer_pg.py`