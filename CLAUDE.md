# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**itop-ai-assistant** — Python middleware that adds an AI layer on top of the
[Combodo iTop](https://www.itophub.io/) ITSM platform. iTop remains the system
of record; this service adds intelligence between users and engineers.

**Current MVP scope:** receive iTop webhooks, validate ticket completeness via
LLM, ask the user clarifying questions via iTop public log if needed.

**Broader vision (next phases):**
- Pattern analysis across tickets (background jobs)
- Knowledge base maintenance automation
- AI-assisted Change Management review
- Engineer-facing contextual widget in iTop UI

## Architecture Principles

**Stateless middleware.** This service does not store ticket state. Ticket status
and conversation history live in iTop and are fetched fresh on every webhook.

**AI acts as a named iTop user.** All comments written to iTop are posted on
behalf of a dedicated iTop service account (e.g. `ai-assistant`). This allows
the service to distinguish its own comments from engineer and user comments when
reading ticket history.

**Act only on "New" tickets.** Before any action, check ticket status via iTop
API. If an engineer has already picked up the ticket (status changed from "New"),
skip processing silently.

**One clarifying question at a time.** If the ticket description is incomplete,
post exactly one focused question to the public log. Do not overwhelm the user
with multiple questions at once.

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

**Docker (full stack — iTop + assistant):**
```bash
cd docker && docker-compose up -d
```

## Architecture

### Request Flow

1. iTop sends `POST /webhook` with `{id, class, async}` payload
2. Router fetches the full ticket object from iTop API
3. For `UserRequest`/`Incident`: also fetches related `Service` and
   `ServiceSubcategory`
4. **Check ticket status** — if not "New", stop processing
5. LLM (`ITopInfoChecker`) evaluates whether description is complete
6. If incomplete: post one clarifying question as a public log entry via
   `ITopClient.update_object()`
7. If complete: optionally enrich ticket fields (category, priority) — planned

Async mode (`async: true`): returns HTTP 202 immediately, processing continues
in background via `asyncio.create_task`.

### Key Source Files

| File | Role |
|------|------|
| `src/main.py` | FastAPI app init, env loading, logging setup |
| `src/router.py` | Webhook endpoint, orchestration, async/sync branching |
| `src/agent.py` | `ITopInfoChecker` — LLM call, result parsing |
| `src/prompts.py` | System + user prompt templates |
| `src/itop/client.py` | `ITopClient` — iTop REST API wrapper |

### LLM Stack

**Current:** LangChain `ChatOpenAI` pointed at any OpenAI-compatible endpoint
via `LLM_BASE_URL` env var (e.g. LM Studio locally, LiteLLM Proxy in prod).

**Direction:** add **LiteLLM Proxy** as a provider-agnostic routing layer +
**LangGraph** (stateful agent graphs) as complexity grows. Keep this in mind
when adding new agent logic — prefer LangGraph patterns over plain LangChain
chains for anything with branching or multi-step logic.

### Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `ITOP_URL` | iTop REST API base URL |
| `ITOP_USER` / `ITOP_PWD` | iTop credentials (alternative: `ITOP_TOKEN`) |
| `ITOP_AI_USER` | iTop username the AI posts comments as |
| `LLM_BASE_URL` | OpenAI-compatible endpoint (LM Studio, LiteLLM Proxy, OpenAI, etc.) |
| `LLM_MODEL` | Model name as exposed by the endpoint |
| `LLM_API_KEY` | API key for the endpoint (`lm-studio` for LM Studio, real key for cloud) |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` | Optional LangSmith observability |

See `docker/.env.dist` for a full template with examples for each provider.

## Testing Notes

- Tests live in `assistant/test/unit/`
- `pytest.toml` sets `pythonpath = ["src"]` and `importmode = importlib`
- LLM calls and HTTP requests are mocked — no real iTop or Google API needed
- Each test file covers one module: `test_agent.py`, `test_router.py`,
  `test_itop_client.py`