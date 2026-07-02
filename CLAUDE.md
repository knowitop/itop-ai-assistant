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
live in Redis with a 30-day TTL. This is the only state the service owns.

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
| `src/main.py`                                   | FastAPI app init, logging setup                     |
| `src/config.py`                                 | `Settings` — centralized config (pydantic-settings) |
| `src/webhook/router.py`                         | Webhook endpoint, async dispatch                    |
| `src/webhook/handler.py`                        | Webhook event processing logic                      |
| `src/graph/enrichment/graph.py`                 | LangGraph graph definition and compilation          |
| `src/graph/enrichment/nodes/guard.py`           | Pre-check node (ai_done, ticket status)             |
| `src/graph/enrichment/nodes/classify.py`        | LLM classification node (Service/ServiceSubcategory)|
| `src/graph/enrichment/nodes/evaluate.py`        | LLM completeness evaluation node                   |
| `src/graph/enrichment/nodes/ask.py`             | Post clarifying question node                       |
| `src/graph/enrichment/nodes/enrich.py`          | Ticket enrichment node                              |
| `src/graph/enrichment/nodes/utils.py`           | `strip_thinking`, `bind_oql`, `html_to_markdown`    |
| `src/graph/enrichment/prompts.py`               | All prompt templates (evaluate, enrich, classify)   |
| `src/state/ticket_state.py`                     | Redis-backed `TicketState` and `TicketStateManager` |
| `src/itop_client/itop.py`                       | `Itop` — iTop REST API wrapper                      |
| `src/itop/client.py`                            | `itop_client` singleton factory                     |

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
Priority (high → low): env vars → `.env` file → `config.yaml` → field defaults.

`config.yaml` (committed to repo) holds non-secret defaults. Secrets and
environment-specific values go in `.env` (not committed).

| Field | Required | Purpose |
|-------|----------|---------|
| `itop_url` | default | iTop REST API base URL |
| `itop_user` + `itop_pwd` | one of | iTop basic auth |
| `itop_token` | one of | iTop token auth (alternative to user+pwd) |
| `llm_base_url` | default | OpenAI-compatible endpoint |
| `llm_model` | **required** | Model name as exposed by the endpoint |
| `llm_api_key` | optional | API key (omit for local LM Studio) |
| `redis_url` | default | Redis connection URL |
| `log_level` | default `INFO` | Logging level |

See `docker/.env.dist` for a full template.

## Testing Notes

- Tests live in `assistant/test/unit/`
- `pytest.toml` sets `pythonpath = ["src"]` and `importmode = importlib`
- LLM calls and HTTP requests are mocked — no real iTop or LLM needed
- Redis is mocked with `fakeredis`
- `get_settings()` is cached via `lru_cache`; call `get_settings.cache_clear()`
  in `setUp`/`tearDown` when tests need to control env vars
- Current test files: `test_config.py`, `test_router.py`, `test_ticket_state.py`,
  `test_nodes_classify.py`, `test_nodes_evaluate.py`, `test_nodes_enrich.py`