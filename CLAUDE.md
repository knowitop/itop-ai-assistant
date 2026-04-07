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
metadata. Two fields per ticket — `rounds` (how many clarifying questions AI
has asked) and `ai_done` (whether AI has finished processing) — live in Redis
with a 30-day TTL. This is the only state the service owns.

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

1. iTop sends `POST /webhook` with `{id, class, async}` payload
2. Webhook handler returns HTTP 202 immediately; processing runs in background
   via `asyncio.create_task`
3. Fetch `TicketState` from Redis — if `ai_done: true`, stop immediately
4. Fetch full ticket from iTop API; for `UserRequest`/`Incident` also fetch
   related `Service` and `ServiceSubcategory`; fetch `UserProfile` of caller
5. If ticket status is not "New" (engineer already working), stop silently
6. If `rounds >= 2`, skip LLM evaluation — enrich with available data and mark
   done
7. LLM evaluates whether ticket description is sufficient for the engineer
8. **If incomplete:** post one clarifying question as a public log entry via
   `ITopClient.add_public_comment()`, increment `rounds` in Redis
9. **If complete (or rounds exhausted):** update ticket fields (subcategory,
   priority), post structured internal note for the engineer, set `ai_done:
   true` in Redis

### Key Source Files

| File                          | Role                                                |
|-------------------------------|-----------------------------------------------------|
| `src/main.py`                 | FastAPI app init, env loading, logging setup        |
| `src/webhook/router.py`       | Webhook endpoint, async dispatch                    |
| `src/graph/graph.py`          | LangGraph graph definition and compilation          |
| `src/graph/nodes/evaluate.py` | LLM completeness evaluation node                    |
| `src/graph/nodes/ask.py`      | Post clarifying question node                       |
| `src/graph/nodes/enrich.py`   | Ticket enrichment node                              |
| `src/state/ticket_state.py`   | Redis-backed `TicketState` and `TicketStateManager` |
| `src/itop_client/itop.py`     | `Itop` — iTop REST API wrapper                      |

### LLM Stack

**LiteLLM** as provider-agnostic LLM client — routes to any OpenAI-compatible
endpoint via `LLM_BASE_URL` (LM Studio locally, LiteLLM Proxy or direct cloud
API in production).

**LangGraph** for all agent logic with branching or multi-step flow. Avoid
plain LangChain chains for anything beyond a single LLM call.

**Langfuse** for observability. All LLM calls are wrapped in Langfuse traces.
Each webhook invocation produces one trace with `ticket_id` as the trace name;
all nodes within that invocation share the same `trace_id`.

### Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `ITOP_URL` | iTop REST API base URL |
| `ITOP_USER` / `ITOP_PWD` | iTop credentials (alternative: `ITOP_TOKEN`) |
| `ITOP_AI_USER` | iTop username the AI posts comments as |
| `LLM_BASE_URL` | OpenAI-compatible endpoint |
| `LLM_MODEL` | Model name as exposed by the endpoint |
| `LLM_API_KEY` | API key (`lm-studio` for LM Studio, real key for cloud) |
| `REDIS_URL` | Redis connection URL (e.g. `redis://localhost:6379/0`) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse observability |
| `LANGFUSE_HOST` | Langfuse server URL |

See `docker/.env.dist` for a full template with examples for each provider.

## Testing Notes

- Tests live in `assistant/test/unit/`
- `pytest.toml` sets `pythonpath = ["src"]` and `importmode = importlib`
- LLM calls and HTTP requests are mocked — no real iTop or LLM needed
- Redis is mocked with `fakeredis`
- Each test file covers one module: `test_evaluate.py`, `test_ask.py`,
  `test_enrich.py`, `test_router.py`, `test_itop_client.py`,
  `test_ticket_state.py`