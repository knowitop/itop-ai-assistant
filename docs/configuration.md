# Configuration

Settings resolve in priority order: **runtime overrides (setup API / admin UI, stored in Redis) → environment variables / `.env` → built-in defaults**.

Environment variables are the IaC-friendly path; the setup API edits the same settings at runtime without a restart. Only the bootstrap values (`REDIS_URL`, `LOG_LEVEL`, `PROMPTS_DIR`) are env-only and require a restart to change.

A full `.env` template with examples is in [`docker/.env.dist`](../docker/.env.dist).

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ITOP_URL` | yes — env or setup API | iTop REST API URL |
| `ITOP_USER` + `ITOP_PWD` | one of — env or setup API | iTop basic auth — use this or `ITOP_TOKEN` |
| `ITOP_TOKEN` | one of — env or setup API | iTop application/personal token — use this or basic auth |
| `LLM_PROVIDER` | default `openai_compatible` | How the model is reached — see [Supported LLM providers](#supported-llm-providers) |
| `LLM_BASE_URL` | depends on provider | Endpoint URL; unused by `openai` and `google_genai` |
| `LLM_MODEL` | yes — env or setup API | Model name as exposed by the endpoint |
| `LLM_API_KEY` | depends on provider | Required by cloud providers; local servers ignore it |
| `LLM_PARAMS` | optional | JSON passed to the client as-is: `{"temperature": 0.2, "max_tokens": 2048}` |
| `LLM_SUPPORTS_FORCED_TOOL_CHOICE` | optional | Only for `openai_compatible` — see [Forced tool calls](#forced-tool-calls) |
| `WEBHOOK_TOKEN` | recommended | Shared secret for `/webhook`; iTop must send it in `X-Auth-Token`. Unset = unauthenticated |
| `ADMIN_TOKEN` | recommended | Bearer token for `/api` admin endpoints. Unset = open (first-run mode) |
| `REDIS_URL` | default `redis://redis:6379` | Redis connection URL (env-only, requires restart) |
| `PROMPTS_DIR` | optional | Directory with prompt file overrides (env-only) — see [Customizing prompts](prompts.md) |
| `LOG_LEVEL` | default `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (env-only) |

> [!NOTE]
> Runtime overrides (including secrets set through the setup API) live in Redis. The bundled `docker-compose.yml` enables Redis persistence (`appendonly yes` + volume) so they survive restarts. To recover a lost admin token, set `ADMIN_TOKEN` in `.env` and restart, or delete the `config:security` key in Redis.

---

## Intake module settings

`intake` is the ticket-processing module: it classifies the ticket, asks at most one clarifying question at a time, and hands the ticket to an engineer with an internal note — all as a single tool-calling agent.

Set in the [Admin UI → Modules](admin-ui.md#modules) or via `PUT /api/config/intake`.

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Enable or disable the intake module |
| `classes` | `["UserRequest", "Incident"]` | Ticket classes to process |
| `max_rounds` | `2` | Max completeness clarifying questions per ticket |
| `max_classify_rounds` | `2` | Max classification clarifying questions per ticket |
| `max_iterations` | `8` | Budget of model calls per ticket; on exhaustion the run is closed with the fallback note |
| `model` | _(global LLM model)_ | Override model for the whole module — the agent needs reliable tool calling |
| `classify_fallback_note` | `Could not determine the request category. Manual classification required.` | Internal note when the ticket stays unclassified |
| `handoff_fallback_note` | `AI intake finished without a summary. Manual review required.` | Internal note when the agent ends without a question or a handoff |

> [!IMPORTANT]
> `enabled` and `classes` are read at **startup**, not per ticket: changing them in the admin UI does not re-route webhooks until the service restarts. Every other setting applies from the next ticket.

Every run leaves a trace in [Admin UI → Runs](admin-ui.md#runs) (`GET /api/runs`): one `agent` step per model turn (the tools it called and with which arguments), one `tool:<name>` step per result (`[success]` / `[error]` plus the text), and a final `usage` step with model calls, tokens in/out and wall time.

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
