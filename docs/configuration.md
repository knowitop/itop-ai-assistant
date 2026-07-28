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

## Enrichment module settings

These are set in the [Admin UI → Modules](admin-ui.md#modules) or via `PUT /api/config/enrichment`.

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Enable or disable the enrichment module |
| `classes` | `["UserRequest", "Incident"]` | Ticket classes to process |
| `max_rounds` | `2` | Max completeness clarifying questions per ticket |
| `max_classify_rounds` | `2` | Max classification clarifying questions per ticket |
| `classify_model` | _(global LLM model)_ | Override model for the classification step |
| `evaluate_model` | _(global LLM model)_ | Override model for the evaluation step |
| `enrich_model` | _(global LLM model)_ | Override model for the enrichment step |

---

## Intake module settings (experimental)

`intake` does the same job as `enrichment` — classify, ask, hand off — but as a single tool-calling agent instead of a fixed graph. It exists to be compared against `enrichment` on real tickets; see [A/B: enrichment vs intake](#ab-enrichment-vs-intake) below. Off by default.

Set in the [Admin UI → Modules](admin-ui.md#modules) or via `PUT /api/config/intake`.

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable or disable the intake module |
| `classes` | `["Incident"]` | Ticket classes to process — must not overlap with `enrichment.classes` |
| `max_rounds` | `2` | Max completeness clarifying questions per ticket |
| `max_classify_rounds` | `2` | Max classification clarifying questions per ticket |
| `max_iterations` | `8` | Budget of model calls per ticket; on exhaustion the run is closed with the fallback note |
| `model` | _(global LLM model)_ | Override model for the whole module — the agent needs reliable tool calling |
| `classify_fallback_note` | _(same text as enrichment)_ | Internal note when the ticket stays unclassified |
| `handoff_fallback_note` | … | Internal note when the agent ends without a question or a handoff |

> [!IMPORTANT]
> `enabled` and `classes` are read at **startup**, not per ticket: changing them in the admin UI does not re-route webhooks until the service restarts (the same is true for `enrichment`). Every other setting applies from the next ticket.
>
> If both modules claim the same class, the routes stay with `enrichment` and intake logs a warning — the service still boots.

### A/B: enrichment vs intake

1. Split the classes so nothing overlaps, e.g. in `config.yaml`:
   ```yaml
   enrichment: { classes: ["UserRequest"] }
   intake:     { enabled: true, classes: ["Incident"] }
   ```
2. Restart the service. `/api/modules` (Admin UI → Modules) must list both.
3. Create two equivalent tickets in iTop — one `UserRequest`, one `Incident` — with the same text.
4. Compare the two runs in [Admin UI → Runs](admin-ui.md#runs) (`GET /api/runs`):
   - `module` tells which pipeline handled the ticket;
   - intake runs log one `agent` step per model turn (the tools it called and with which arguments), one `tool:<name>` step per result (`[success]` / `[error]` plus the text), and a final `usage` step with model calls, tokens in/out and wall time;
   - enrichment runs log one step per graph node.
5. Compare the outcome in iTop as well: the classification set, and the note in the private log or the question in the public log.

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

**Per-node model overrides** in the Modules settings allow using a smaller/faster model for classification and a stronger one for enrichment — useful if your LiteLLM Proxy or local server exposes multiple models.
