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
| `LLM_BASE_URL` | yes — env or setup API | OpenAI-compatible LLM endpoint URL |
| `LLM_MODEL` | yes — env or setup API | Model name as exposed by the endpoint |
| `LLM_API_KEY` | optional | API key — omit for local LM Studio |
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

The assistant works with any **OpenAI-compatible endpoint**. Set `LLM_BASE_URL` and `LLM_MODEL` to connect:

| Provider | Base URL | Notes |
|----------|----------|-------|
| **LM Studio** (local) | `http://localhost:1234/v1` | No API key needed; start a local server in LM Studio |
| **Ollama** (local) | `http://localhost:11434/v1` | Set `LLM_API_KEY=ollama` |
| **OpenAI** | `https://api.openai.com/v1` | Requires `LLM_API_KEY=sk-...` |
| **Azure OpenAI** | `https://<resource>.openai.azure.com/` | Use deployment name as model |
| **LiteLLM Proxy** | `http://litellm:4000/v1` | Fronts any provider; any string as key |
| Any other | any OpenAI-compatible URL | Works if the endpoint supports `/chat/completions` |

**Reasoning models** (DeepSeek-R1, Qwen3, etc.) are supported out of the box — the assistant strips `<think>…</think>` blocks from responses before processing them. The stripped tag names are configurable in the LLM settings (`Think Tags` in the UI, or `LLM_THINK_TAGS` env var).

**Tool calling is a hard requirement.** The module runs as one agent loop, so it uses a single model (`model` in the Modules settings, or the global `LLM_MODEL`) and that model must call tools reliably — one that answers in prose instead of calling a tool wastes the run and closes the ticket with a fallback note.
