# Configuration

Settings resolve in priority order: **runtime overrides (setup API / admin UI, stored in Redis) → environment variables → `.env` → `assistant/config.yaml` → built-in defaults**.

Environment variables are the IaC-friendly path; the setup API edits the same settings at runtime without a restart. Only the bootstrap values (`REDIS_URL`, `QDRANT_URL`, `LOG_LEVEL`, `PROMPTS_DIR`, `UI_DIST_DIR`) are env-only and require a restart to change.

A full `.env` template with examples is in [`docker/.env.dist`](../docker/.env.dist). `assistant/config.yaml` is committed to the repository and holds non-secret defaults — it is the convenient place for structured values like `LLM_PARAMS`, which env vars can only express as a JSON string.

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
| `WEBHOOK_TOKEN` | recommended | Shared secret for `/webhook`; iTop must send it in `X-Auth-Token`. Unset = unauthenticated |
| `ADMIN_TOKEN` | recommended | Bearer token for `/api` admin endpoints. Unset = open (first-run mode) |
| `REDIS_URL` | default `redis://localhost:6379` | Redis connection URL (env-only, requires restart); the compose stack sets `redis://redis:6379` |
| `STATE_TTL_DAYS` | default `30` | How long per-ticket AI state lives in Redis |
| `RUN_TTL_DAYS` | default `7` | How long the run journal keeps a processing run |
| `QDRANT_URL` | optional | Qdrant URL for the vector index (`http://host:6333`), env-only. Unset = Redis-only — see [Vector index](#vector-index) |
| `EMBEDDINGS_BASE_URL` / `EMBEDDINGS_MODEL` / `EMBEDDINGS_API_KEY` | optional | OpenAI-compatible `/v1/embeddings` endpoint for the vector index |
| `EMBEDDINGS_DIMENSION` | default `1024` | Vector dimension — must match what the model returns |
| `EMBEDDINGS_BATCH_SIZE` | default `32` | Texts per embeddings request |
| `PROMPTS_DIR` | optional | Directory with prompt file overrides (env-only) — see [Customizing prompts](prompts.md) |
| `UI_DIST_DIR` | optional | Directory with the built admin SPA (env-only). The Docker image sets it; running from a source checkout finds `ui/dist` on its own |
| `LOG_LEVEL` | default `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (env-only) |

> [!NOTE]
> Runtime overrides (including secrets set through the setup API) live in Redis. The bundled `docker-compose.yml` enables Redis persistence (`appendonly yes` + volume) so they survive restarts. To recover a lost admin token, set `ADMIN_TOKEN` in `.env` and restart, or delete the `config:security` key in Redis.

---

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
| `enabled` | `true` | Enable or disable the intake module |
| `classes` | `["UserRequest", "Incident"]` | Ticket classes to process |
| `max_rounds` | `2` | Max completeness clarifying questions per ticket |
| `max_classify_rounds` | `2` | Max classification clarifying questions per ticket |
| `max_iterations` | `9` | Budget of model calls per ticket; on exhaustion the run is closed with the fallback note |
| `model` | _(global LLM model)_ | Override model for the whole module — the agent needs reliable tool calling |
| `classify_fallback_note` | `Could not determine the request category. Manual classification required.` | Internal note when the ticket stays unclassified |
| `handoff_fallback_note` | `AI intake finished without a summary. Manual review required.` | Internal note when the agent ends without a question or a handoff |
| `similar_max_age_days` | `365` | How far back solved tickets may be quoted in the handoff note |
| `similar_candidates` | `15` | Candidates read from the index before iTop is asked which of them the run may see |
| `similar_top` | `5` | Max references in one handoff note |

> [!NOTE]
> The three `similar_*` settings only do something when the [vector index](#vector-index) is switched on and an embeddings endpoint is configured. Without that, the agent is not given the search tool at all and the handoff note carries no references.

> [!IMPORTANT]
> `enabled` and `classes` are read at **startup**, not per ticket: changing them in the admin UI does not re-route webhooks until the service restarts. Every other setting applies from the next ticket.

Every run leaves a trace in [Admin UI → Runs](admin-ui.md#runs) (`GET /api/runs`): one `agent` step per model turn (the tools it called and with which arguments), one `tool:<name>` step per result (`[success]` / `[error]` plus the text), and a final `usage` step with model calls, tokens in/out and wall time.

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

Adding a provider is a single entry in `assistant/src/llm_providers.py` — the setup API and the UI form are generated from that registry.

**Model parameters.** Anything the provider's client accepts goes in `LLM_PARAMS` (env, JSON) or the **Model parameters** field in Connections: `{"temperature": 0.2, "max_tokens": 2048, "timeout": 60}`. Connection fields (`model`, `base_url`, `api_key`) are rejected there — set them in their own fields.

**Reasoning models** (DeepSeek-R1, Qwen3, etc.) are supported out of the box — the assistant strips `<think>…</think>` blocks from responses before processing them. The stripped tag names are configurable in the LLM settings (`Think Tags` in the UI, or `LLM_THINK_TAGS` env var).

**Tool calling is a hard requirement.** The module runs as one agent loop, so it uses a single model (`model` in the Modules settings, or the global `LLM_MODEL`) and that model must call tools reliably — one that answers in prose instead of calling a tool wastes the run and closes the ticket with a fallback note. **Test LLM** checks this: it asks the model to call a probe tool and reports if it did not.

### Forced tool calls

The intake agent has no way to deliver plain text — the requester only sees what a tool posts — so where the endpoint allows it, the agent forbids prose outright by forcing a tool call. OpenAI and Gemini accept that; Ollama ignores it; DeepSeek rejects it with HTTP 400 ("Thinking mode does not support this tool_choice"). For the named providers the answer is built in and there is nothing to configure.

`openai_compatible` is the exception: the same setting fronts vLLM (accepts) and DeepSeek (rejects), so the deployment owner has to answer. Tick **Endpoint accepts a forced tool_choice** in Connections (or set `LLM_SUPPORTS_FORCED_TOOL_CHOICE=true`) and press **Test LLM** — the probe forces the choice and tells you whether the server took it. Leave it off if unsure: the agent then retries a prose answer once instead, which costs an extra model call but works everywhere.

---

## Vector index

Optional infrastructure: a semantic index of iTop objects in Qdrant, built for upcoming features (similar tickets, KB matching, pattern analysis). **Nothing in the current intake flow reads it** — leave `QDRANT_URL` unset and the whole subsystem stays off, with the assistant running Redis-only exactly as before.

To switch it on:

1. Point `QDRANT_URL` at a Qdrant instance — the compose stack ships one (`http://qdrant:6333`). Collections are created lazily on first use; a failure is logged as a warning and never blocks the boot.
2. Configure the **Embeddings** connection (see [Admin UI → Connections](admin-ui.md#embeddings-tab)). The model must be **multilingual** — tickets are rarely single-language. `EMBEDDINGS_DIMENSION` must match what the model returns; **Test embeddings** measures the real dimension and tells you if it does not.
3. Turn indexing on in [Admin UI → Vector index](admin-ui.md#vector-index) and configure per-class settings there.

A background sweep picks up objects changed since the last pass, splits them into chunks per the class's chunking profile, and embeds only what actually changed (content-hash guard). The index stores **embeddings, ids and filter metadata only — never ticket text**; anything shown to a user is re-fetched fresh from iTop, so the index is a rebuildable cache, not a copy of your ticket database.

Changing the embeddings model or its dimension invalidates every stored vector — vectors from different models are not comparable. The assistant refuses to mix them and asks for a full reindex instead (**Reindex** in the UI, or `uv run itop-ai-reindex --full` next to the deployment).
