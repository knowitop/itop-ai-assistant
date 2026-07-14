# Stack Improvements — Quick-Boost Review

Where the current stack and its self-imposed constraints are worth keeping,
where a small change buys a lot, and the one architectural fork worth
deciding *before* the vector store gets built. Ordered by leverage
(value ÷ effort), not by size.

Status: **recommendation note.** Nothing here is committed; §1 items are
safe to do now, §2 is a decision to make deliberately.

---

## 0. What NOT to touch

These constraints look like limitations but are load-bearing — keep them:

- **Minimal frontend deps** (`useState` + one `fetch` wrapper, no Redux/
  TanStack). Correct for an AI-maintained SPA owned by a non-frontend dev.
  (One earmarked exception in §3.)
- **DI without module singletons** (`build_deps`, `GraphContext` per run).
  This is what makes runtime config edits and per-node model overrides work
  cleanly — don't trade it for globals.
- **Vendored, application-agnostic iTop client.** Keeps the REST quirks in
  one place. Leave it generic.
- **Prompts as files + Redis overrides.** Simple, hot-reloadable, already
  validated. No prompt-framework needed.

The point below is not "add more" — it's three cheap wins and one honest
datastore decision.

---

## 1. Three quick wins — no new datastore, high leverage

### 1.1 CI that runs the tests (biggest process gap)

Today `.github/workflows/` has **only `docker-publish.yml`** — the extensive
test suite, `ruff`, and `mypy` never run on a push or PR. The image publishes
whether or not tests pass.

- Add a `ci.yml`: `uv sync`, `ruff check`, `ruff format --check`, `mypy src`,
  `pytest --cov`; matrix on the one supported Python. Gate
  `docker-publish` on it.
- Effort: ~1 hour. Payoff: every future plan below lands with a safety net
  instead of on trust. **Do this first, before writing any new module.**

### 1.2 Structured LLM output — kill the fragile text parsing

Every plan so far has a "degrade gracefully when the LLM output doesn't
parse" clause (classify, rerank, cluster labeling, digest). That fragility is
self-inflicted: the stack currently uses **plain-text responses only**.

- **Not a universal client-side feature.** `ChatOpenAI.with_structured_output`
  delegates to a *server-side* mechanism — `function_calling` (default),
  `json_mode`, or `json_schema` — and works only where the endpoint/model
  implements the chosen one. It is not "typed output for any model". A model
  without tool/JSON support (historically DeepSeek-R1 before R1-0528; some
  local models) will fail or return garbage under the default method.
- **Reasoning models are the real snag** here, not DeepSeek specifically.
  This service targets arbitrary OpenAI-compatible endpoints, including local
  reasoning models (R1, Qwen3) via LM Studio. Two realities:
  - *Local (llama.cpp/LM Studio)* can grammar-constrain output to a JSON
    schema for **any** model — so structured output is achievable more widely
    than "does the model support tools".
  - *But* hard-constraining a reasoning model from the first token suppresses
    its `<think>` reasoning (quality drop) unless the grammar first admits the
    think block. This is exactly why `strip_thinking` exists. Correct pattern
    for reasoning models: **let it think → `strip_thinking` → parse JSON from
    the tail**, not a token-0 grammar constraint.
- **So the recommendation is "typed parsing with a robust fallback", not
  "structured output everywhere".** Use `with_structured_output` where the
  endpoint supports it; elsewhere fall back to a "return JSON" prompt +
  tolerant Pydantic parse after `strip_thinking` — the mode that works across
  the widest range of local/reasoning endpoints. `instructor` is a drop-in
  only if the tolerant parse proves insufficient.
- Either way it removes the "LLM returned prose, we regex it" class of bugs
  from classify/rerank/insights and makes those contracts explicit.
- Effort: incremental, per node. No new dependency for the baseline path.

### 1.3 LLM tracing/observability — see inside the black box

An agentic, multi-node, prompt-tuned service with **no request tracing** is
hard to debug and impossible to improve empirically. The `RunJournal` records
*that* a step ran, not the prompt/response/tokens/latency.

- **Langfuse** (self-hostable, OpenAI-compatible + LangChain callback) or
  **LangSmith** (SaaS, first-party LangChain). Langfuse fits the
  self-hosted, on-prem-friendly posture better; it does add a Postgres —
  which §2 argues you may want anyway.
- Wire it as a LangChain callback in `create_llm`; gate by config (off by
  default, no egress unless enabled).
- Payoff: prompt iteration by data, token/cost visibility, latency
  attribution — directly accelerates every prompt-heavy plan (enrichment,
  widget, insights).

---

## 2. The one real fork: keep everything on Redis, or add Postgres?

> **Decision (2026-07-07): Option B — add Postgres + `pgvector`.** Redis stays
> for operational state (ticket state, locks, config/prompt overrides, run
> journal); Postgres becomes the vector + analytical store. Gated behind
> `vector.enabled` / `insights.enabled` so the base deployment stays
> Redis-only until a customer turns those features on. Rationale and the
> honest counter-arguments are kept below for the record; [vector-store.md](vector-store.md)
> now targets pgvector.

The [vector-store](vector-store.md) and [pattern-analysis](pattern-analysis.md)
plans originally leaned on "Redis 8 has vector search, so no new datastore".
That keeps the stack tiny and is *legitimate* — but notice how much was being
bent to fit Redis:

- vector index with metadata filters (`FT.*`),
- trend **baselines** in rolling hashes,
- **reports** as TTL'd JSON blobs that *cannot be aggregated historically*
  (pattern-analysis §6 already flags "a deployment wanting a year of digests
  should export" — i.e. Redis can't answer it),
- ACL pre-filter tags,
- and `fakeredis` **can't emulate `FT.*`**, so the vector layer has no unit
  tests (vector-store Stage 1 concedes this).

All of these are Postgres's home turf.

### Option A — stay Redis-only (as the plans assume)

Pros: zero new infra, one stateful service, smallest ops surface. Best if
analytics stay ephemeral (rolling digests, no year-long history) and scale
stays ≤ ~10⁵–10⁶ vectors.

Cons: reports aren't queryable history; vector layer is integration-test-only
(needs Testcontainers, §3); everything competes for one Redis's memory
(operational state + vectors + baselines).

### Option B — add Postgres + `pgvector` as the analytical/vector store

Redis keeps what it's genuinely best at (operational ticket state, locks,
config/prompt overrides, run journal — all short-lived, hot). Postgres takes:

- **vector search** (`pgvector`, HNSW, metadata filter in the same `WHERE`,
  ACL join) — mature, testable, and the ACL filter from vector-store §4
  becomes a SQL predicate instead of hand-built `FT.*` tag strings;
- **trend aggregation** — real `GROUP BY … date_trunc`, weekday baselines as
  a query, not hand-rolled EWMA hashes;
- **durable reports/insights** — queryable for a year, the "export path"
  problem disappears;
- unit-testable with Testcontainers (§3), and Langfuse (§1.3) already wants a
  Postgres.

Cons: a new stateful service in compose, first migrations (Alembic),
`asyncpg`/SQLAlchemy added. For a project with *zero* relational storage
today, that's a real step — but a one-time one, and it collapses four
Redis-contortions into one mature store.

**Decision: Option B.** The decisive reasons are testability (pgvector is
unit-testable via Testcontainers; `fakeredis` can't emulate `FT.*` at all) and
the historical-analytics roadmap (pattern-analysis needs real `GROUP BY` /
`date_trunc`, which Redis structurally can't answer). Vector search itself and
the ACL pre-filter are *not* the deciding factors — Redis 8 would have handled
the scale, and the authoritative ACL check (layer 2) lives in iTop regardless
of backend. B is chosen because it collapses vector + analytics + Langfuse's
Postgres requirement into one mature store, gated behind
`vector.enabled`/`insights.enabled` so the base deployment stays Redis-only
until those features are switched on.

---

## 3. Situational — adopt when the triggering feature lands, not before

- **Testcontainers-python** (dev dep): real Redis/Postgres in tests. Becomes
  necessary the moment vector search exists (`fakeredis` can't do `FT.*`;
  pgvector needs real PG). Add it *with* Stage 1 of the vector store, not
  speculatively.
- **TanStack Query** (frontend): the deliberate "plain fetch" rule starts to
  hurt exactly on the **polling** pages the plans add — Runs, Insights, and
  the widget's async results. When you find yourself hand-writing
  poll+cache+invalidate three times, that's the signal to relax the rule for
  *those* screens only. Not before.
- **SSE for streaming** (widget summary/draft): deferred in the widget plan
  because of the PHP proxy. Revisit only if engineers complain about
  wait-time on long generations; a chunked-proxy pass or an opt-in direct-CORS
  mode is the unlock. Low priority.
- **ARQ** (Redis async task queue) vs. the hand-rolled jobs framework
  (pattern-analysis Stage 0): the custom runner is small and fits the DI
  model, so build it first. Reach for ARQ only if you need retries, multiple
  workers, and scheduled fan-out beyond what a single-process asyncio loop
  with a Redis lease gives you.

---

## 4. Suggested sequencing

1. **CI (§1.1)** — 1 hour, unblocks safe iteration on everything else.
2. **Structured output (§1.2)** — refactor existing enrichment nodes; sets the
   contract style all new LLM code will copy.
3. **Datastore fork (§2) — decided: Postgres + `pgvector`.** Stand up the
   Postgres service, `asyncpg`/SQLAlchemy, and the first Alembic migration as
   vector-store Stage 0 so the rest of that plan targets pgvector directly.
4. **Tracing (§1.3)** — right after the fork (Langfuse's Postgres dep folds
   into an Option-B decision).
5. Everything in §3 lands **with** its triggering feature, never ahead of it.

The theme: two of the three quick wins (CI, structured output) cost nothing
in new infra and remove real, recurring pain; the datastore fork is the only
place where *relaxing* the "keep the stack tiny" constraint is likely to pay
for itself — and it's worth deciding on purpose rather than by default.
