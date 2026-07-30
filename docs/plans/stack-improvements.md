# Stack Improvements — Quick-Boost Review

Where the current stack and its self-imposed constraints are worth keeping,
and where a small change buys a lot. Ordered by leverage (value ÷ effort),
not by size.

Status: **recommendation note.** Nothing here is committed. Two of the
original items have since been settled and are recorded at the bottom rather
than re-argued.

---

## 0. What NOT to touch

These constraints look like limitations but are load-bearing — keep them:

- **Minimal frontend deps** (`useState` + one `fetch` wrapper, no Redux/
  TanStack). Correct for an AI-maintained SPA owned by a non-frontend dev.
  (One earmarked exception in §3.)
- **DI without module singletons** (`build_deps`, an `IntakeContext` built per
  run). This is what makes runtime config edits and the per-module model
  override work cleanly — don't trade it for globals.
- **Vendored, application-agnostic iTop client.** Keeps the REST quirks in
  one place. Leave it generic.
- **Prompts as files + Redis overrides.** Simple, hot-reloadable, already
  validated. No prompt-framework needed.
- **Invariants in tool code, not in prompt text.** The intake tools reject a
  bad call with an instruction on what to do instead; the round counters are
  picked by code. Every future agent should copy that split rather than
  asking a model to behave.

---

## 1. Quick wins

### 1.1 CI that runs the tests — **done**

Was the biggest process gap: `.github/workflows/` held only
`docker-publish.yml`, so the test suite, `ruff` and `mypy` never ran on a push
or a PR, and the image published whether or not tests passed.

`ci.yml` now runs on push to `main` and on every PR, in two parallel jobs:

- **python** (`assistant/`): `uv sync --frozen` → `ruff check` →
  `ruff format --check` → mypy → `pytest --cov` → `pytest test/pg`.
  Single Python (3.13), no matrix — one version is supported.
- **ui**: `npm ci` + `npm run build`, which is `tsc --noEmit && vite build` —
  the TypeScript gate the release build depends on.

Three details worth keeping in mind when touching it:

- mypy runs as `pre-commit run mypy --all-files`, not `uv run mypy src`. The
  hook runs mypy in its own venv without langchain, and is the stricter of the
  two; `uv run mypy src` would pass things CI should catch.
- `test/pg` joins the same job — Testcontainers needs a Docker daemon and the
  GitHub runner has one. `test/integration` needs a real model endpoint and
  stays out of CI.
- `docker-publish.yml` calls `ci.yml` via `workflow_call` and gates
  `build-and-push` on it (`needs: ci`), so a tag cannot publish an image that
  fails the suite.

### 1.2 LLM tracing — see inside the black box

The `RunJournal` improved a lot with the agentic intake module: it records
every model turn (which tools, which arguments), every tool result, and a
final `usage` step with model calls, tokens and wall time. What it still does
*not* record is the prompt and the raw response — so "why did the model
choose that" is answerable only by re-running.

LangChain's own tracing fills exactly that gap and is already reachable:
`docker/.env.dist` ships the `LANGSMITH_*` variables, and LangChain picks
them up with no code. So the cheap step is **documenting** that path, not
building one.

- If SaaS is unacceptable, **Langfuse** (self-hostable, LangChain callback)
  is the alternative; it wants a Postgres, which this deployment now has.
  That would need real wiring — a callback in `create_llm`, gated by config,
  off by default, no egress unless enabled.
- Payoff: prompt iteration by data rather than by anecdote — directly
  accelerates every prompt-heavy plan (intake tuning, widget, insights).

---

## 2. Situational — adopt when the triggering feature lands, not before

- **TanStack Query** (frontend): the deliberate "plain fetch" rule starts to
  hurt exactly on the **polling** pages — Runs, the vector status screen,
  Insights, and the widget's async results. When you find yourself
  hand-writing poll+cache+invalidate a third time, that's the signal to relax
  the rule for *those* screens only. Not before.
- **SSE for streaming** (widget summary/draft): deferred in the widget plan
  because of the PHP proxy. Revisit only if engineers complain about
  wait-time on long generations; a chunked-proxy pass or an opt-in direct-CORS
  mode is the unlock. Low priority.
- **ARQ** (Redis async task queue) vs. the hand-rolled jobs framework
  (pattern-analysis Stage 0): the vector indexer is already a single-process
  asyncio task with a Postgres advisory lock, and generalizing *it* into a
  small job registry is the natural next step. Reach for ARQ only if you need
  retries, multiple workers, and scheduled fan-out beyond what that gives you.

---

## 3. Settled — recorded, not up for re-litigation

**Datastore fork → Postgres + `pgvector`** (decided 2026-07-07, built since).
Redis keeps what it is genuinely best at — operational ticket state, locks,
config/prompt overrides, the run journal, all short-lived and hot. Postgres
owns the vector index and the future analytical tables. The decisive reasons
were testability (`fakeredis` cannot emulate `FT.*` at all, so a Redis vector
layer would have had no unit tests; pgvector is testable via Testcontainers)
and the historical-analytics roadmap (pattern-analysis needs real `GROUP BY` /
`date_trunc`, which Redis structurally cannot answer). Vector search itself
was *not* the deciding factor — Redis 8 would have handled the scale. Gated
behind `database_url` + `vector.enabled`, so the base deployment stays
Redis-only. See [vector-store.md](vector-store.md).

**Testcontainers-python** was adopted alongside it (`test/pg/`, opt-in, needs
Docker) — the trigger condition in the original note was met.

**Structured LLM output** is moot for the path that motivated it. The
original concern was the "degrade gracefully when the LLM output doesn't
parse" clause in every plan — text answers scraped with regexes. The intake
module answers that differently and better: the model does not produce
parseable text at all, it calls tools whose arguments pydantic validates,
and where the endpoint allows it a forced `tool_choice` makes prose
impossible rather than merely correctable. Future LLM code (cluster labeling,
rerank, digests) should reach for a tool call first; `with_structured_output`
remains a fallback for the cases where a tool call genuinely does not fit,
with the caveat that it delegates to a *server-side* mechanism and is not
available on every endpoint. For reasoning models the working pattern is
still "let it think → `strip_thinking` → parse the tail", never a token-0
grammar constraint.
