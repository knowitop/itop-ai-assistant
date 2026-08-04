# CLAUDE.md

This file provides guidance to Claude Code when working with code in this
repository.

## Project Overview

**itop-ai-assistant** — Python middleware that adds an AI layer on top of the
[Combodo iTop](https://www.itophub.io/) ITSM platform. iTop remains the system
of record; this service adds intelligence between users and engineers.

Implemented today is the **intake** module: an iTop webhook on a new ticket →
classification against the service catalog → at most one clarifying question at
a time in the public log → an internal note handing the ticket to an engineer.
Next phases: an engineer-facing console in the iTop UI, pattern analysis across
tickets, knowledge base maintenance, AI-assisted Change Management review.

## Architecture Principles

**iTop is the system of record.** Ticket content, conversation history and user
data always come from iTop. Never cache or duplicate this data locally — read
fresh on every run.

**Redis stores operational state, and only that.** Per-ticket AI state
(`rounds`, `classify_rounds`, `ai_done`), the per-ticket processing lock, the
runtime config/prompt overrides and the run journal. This is the only state the
service owns.

**AI acts as a named iTop user.** Comments are written on behalf of a dedicated
service account, which is what makes AI comments distinguishable without parsing
text. Where a run acts under a delegated principal (an engineer's own token,
sent per request), the account no longer says who acted — so every operation of
a run carries a `comment` naming the module, the run id and the engineer it acts
for. The account attributes, the comment explains.

**Human-in-the-loop by default.** The AI acts autonomously only when confident
and the action is reversible. Asking a question and updating ticket fields are
autonomous; resolving or reassigning a ticket requires engineer confirmation.
When in doubt — do nothing and log the reason.

**One clarifying question at a time**, max two rounds. After that, enrich with
whatever is available and hand off.

**Act only while the ticket is unassigned.** If an engineer has picked it up
(status moved off "New"), stop silently. Check Redis `ai_done` first — if true,
skip without calling iTop at all.

**Never react to our own comments.** Two lines of defence against webhook loops:
iTop trigger contexts exclude `REST/JSON` (documented in README), and
`IntakeRun.stop_reason` stops if the last public log entry was posted by the AI
service account — so a misconfigured trigger degrades to a no-op instead of an
infinite question loop.

## Repository map

| Path | What |
|---|---|
| `assistant/` | the service — Python, FastAPI, uv |
| `assistant/src/itop_ai_assistant/` | the package; file-by-file map in `dev-docs/reference/source-map.md` |
| `ui/` | admin SPA — Vite + React + Mantine |
| `docker/` | compose stack: iTop + assistant + Redis |
| `docs/` | **public** user documentation — setup, configuration, admin UI, prompts |
| `dev-docs/` | **internal** development docs — architecture, references, ADRs, tasks (separate repository) |

## Read before you change

Rules under `.claude/rules/` load by themselves when you open a matching file.
This index is for the case where you are reasoning about an area without having
opened it yet.

| Area | Rule | Background |
|---|---|---|
| Run core, entry points, registry, principal | `core.md` | `dev-docs/architecture/platform.md` §3.1–3.2 |
| Agents, tools, prompts, LLM client | `agents.md` | `dev-docs/architecture/intake.md` |
| iTop calls, repositories, domain model | `itop.md` | `dev-docs/reference/itop-api.md` |
| Vector layer | `vector.md` | `dev-docs/architecture/vector.md` |
| Tests | `testing.md` | — |
| Admin SPA | `ui.md` | `docs/admin-ui.md` |
| Config, setup API, prompt loading | — | `dev-docs/architecture/config-and-setup.md`, `docs/configuration.md` |

The whole architecture in one document — the seams, the three trigger types, the
identity model, the extension points: `dev-docs/architecture/platform.md`.

## Development Commands

All commands run from `assistant/` unless noted.

```bash
uv sync                                # deps (--no-dev for production)
uv run uvicorn itop_ai_assistant.main:app --host 0.0.0.0 --port 8001 --reload
uv run pytest                          # unit tests (the default suite)
uv run pytest -k "test_name"           # one test
uv run ruff check . && uv run ruff format .
cd ../docker && docker-compose up -d   # full stack: iTop + assistant + Redis
```

**Before pushing**, run what CI runs: `uv run pre-commit run --all-files` and
`uv run pytest`. Note that `pre-commit run mypy --all-files` is the **strict**
type gate — `uv run mypy src/` is not the same check and passing it proves
less. CI (`.github/workflows/ci.yml`) runs on every push to `main` and every PR
and gates the image publish; it adds `pytest test/pg` and `npm run build` for the
UI. `test/integration` needs a real model endpoint and is excluded there.

## Configuration

Priority (high → low): Redis runtime overrides (setup/admin API) → env vars →
`.env` → `config.yaml` → field defaults. Every variable is tabulated in
`docs/configuration.md`; `docker/.env.dist` is the full template.

**No field is required at startup** — the app always boots. Until the `itop` and
`llm` sections are complete, `/webhook` returns 503 and the admin API stays open
for the setup wizard. Connection edits apply from the next run without a
restart; the exceptions are `intake.enabled` / `intake.classes` and
`selfcheck.enabled`, which are read at startup because the trigger registry is
built from them.
