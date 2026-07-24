# Pattern Analysis — Architecture & Implementation Plan

Background jobs that surface recurring issues and trends across tickets:
emerging incident storms, recurring problems that deserve a Problem record,
category/volume trends, and KB gaps. Builds directly on the
[vector store](vector-store.md) (embeddings + filter metadata in Postgres/
`pgvector` are the clustering input) and feeds both the admin UI and the
[engineer widget](engineer-widget.md).

Status: **plan** (nothing implemented yet). Depends on vector-store
Stages 1–3; one detector (metadata trends) works without vectors at all.

---

## 1. Use cases, in priority order for a Service Desk

1. **Incident storm / duplicate-burst detection** (near-real-time). Five
   tickets in an hour saying "mail is down" in different words = an outage.
   Detecting it early changes how the desk works that hour: one master
   ticket, one communication, no five parallel investigations.
2. **Recurring-issue mining → Problem candidates** (weekly). The same
   printer/VPN/login issue solved 14 times in a quarter is a Problem record
   nobody has filed. This is the classic ITIL problem-management gap that
   pattern analysis can close.
3. **Volume & category trends** (daily/weekly digest). "Password resets +40%
   week-over-week", "new topic appeared: MFA push fatigue" — management
   visibility with zero LLM cost for the numeric part.
4. **KB gap analysis** (later). Clusters with no matching FAQ article =
   prioritized list of KB articles worth writing; pairs with the widget's
   "KB draft from resolved ticket" feature.

---

## 2. Position in the stack

```
┌─ Postgres (pgvector) ──────────────────────────────────┐
│ vector_chunk_v{N} (embeddings + org/status/service     │  ← vector store (built)
│                    columns)                            │
│ insights_baseline / insights_cluster / insights_report │  ← this plan (durable, queryable)
└──────────────┬─────────────────────────────────────────┘
               │ read vectors & metadata (SQL; no iTop load)
┌──────────────▼─────────────────────────────────────────┐
│ Jobs runner (asyncio, advisory-lock, journal —         │
│ generalized from the vector sweep)                     │
│  • storm detector        (every few minutes)           │
│  • trends & digest       (daily / weekly)              │
│  • problem-candidate miner (weekly)                    │
└──────────────┬─────────────────────────────────────────┘
               │ exemplar re-fetch (iTop, fresh & scoped)
               ▼
 Outputs: reports in Postgres → admin UI "Insights" page,
          internal notes on tickets, HITL "create Problem" action
```

(Redis stays in the stack for operational ticket state, locks, config/prompt
overrides and the run journal — it is simply no longer where insights live.)

Key reuse: the analysis input is **the index we already maintain** — vectors
plus `obj_class/status/service_id/org_id/created_at` columns. Jobs do not
re-read the ticket mass from iTop; only cluster *exemplars* (a handful of
representative tickets per cluster) are fetched fresh for LLM labeling and
display, which also re-applies the "iTop is the source of truth" rule.

**Generalize the sweep runner into a small jobs framework first**
(`src/jobs/`): job registry, per-job interval/daily schedule, Postgres
advisory-lock lease (`pg_try_advisory_lock`, same primitive the vector sweep
now uses), run journal. The vector indexer's sweep becomes the first
registered job; pattern-analysis jobs are the next three. One scheduling
mechanism, one observability story (`GET /api/jobs/status`), no new deployment
unit.

---

## 3. Detection approaches (four, from cheap to rich)

### A. Metadata trend stats — no vectors, no LLM

Counts per (class, service, subcategory, org) per day, compared against a
baseline of previous weeks **with weekday alignment** (Monday compares to
Mondays — service desks are strongly weekly-seasonal). Flag when the z-score
exceeds a configured threshold and the absolute count exceeds a floor
(`min_count`, to silence "300% growth: 1 → 3").

Data source: OQL aggregate reads over recent windows (`SELECT … WHERE
start_date > …`, minimal output_fields, paged) — cheap enough daily; no
dependence on the vector index at all. **The daily counts land in Postgres**
(`insights_counts (env, day, class, service, subcategory, org, n)`), and the
baseline is a *query*, not a hand-rolled rolling hash:
`GROUP BY … , extract(dow FROM day)` with `date_trunc`/window functions over
the trailing weeks. This is the concrete payoff of the datastore fork — the
weekday-aligned baseline and the anomaly comparison are SQL the database is
built for, and the historical counts stay queryable for a year instead of
decaying with a TTL.

### B. Storm detector — KNN threshold, near-real-time

For each ticket entering the index (piggybacks on the vector sweep, so it
runs at sweep freshness — minutes):

```
neighbors = KNN(ticket.profile+description vector,
                filter: created within storm_window (e.g. 4h),
                same class-set, any status)
if count(similarity ≥ storm_threshold) ≥ storm_min_size:
    → open/extend a "storm" record (member ids, centroid, first_seen)
```

Storm records are tracked in an `insights_storm` table with a fingerprint
(centroid hash) so a growing storm **updates one alert instead of re-alerting
per ticket** — the open/extend/close lifecycle is an `UPDATE`, and past storms
stay on record for retrospection instead of expiring.
Actions when a storm opens: report entry + (config-gated) an **internal
note** on member tickets — "N similar tickets in the last 4h, possibly one
underlying issue: [refs]" — posted as the AI service account to
`private_log`, which is within the established autonomous-action envelope
(reversible, engineer-facing). The widget's `context` endpoint also surfaces
"this ticket is part of an active storm" for free.

No new dependencies: it's the retriever's KNN with a time filter — a
`WHERE created_at >= now() - interval` predicate over the `created_at
timestamptz` column the vector-store schema already carries (vector-store
plan §2).

### C. Recurring-issue mining — batch clustering + LLM labeling (weekly)

Weekly job over a sliding window (default 90 days, resolved+closed included):

1. Load `profile`+`description` vectors + metadata for the window from
   Postgres (one filtered `SELECT`; thousands of vectors — MBs, in-process is
   fine).
2. Cluster. Two-phase to stay dependency-light:
   - **greedy leader clustering** on cosine threshold (no deps, O(n·k)) as
     the MVP — good enough to find "the same thing said 14 times";
   - upgrade path: **HDBSCAN** (via `scikit-learn`/`hdbscan` optional
     dependency) when leader clustering's fixed threshold shows its limits —
     it finds variable-density clusters and labels noise honestly. Keep it
     behind an extra `uv` dependency group so the base image stays slim.
3. Filter clusters: `min_size` (default 5), coverage across ≥ N distinct
   callers/orgs (one user filing 12 tickets is a different insight than 12
   users filing one each — both interesting, labeled differently).
4. **Novelty scoring against previous runs**: cluster centroid compared to
   fingerprints stored in `insights_cluster` — recurring clusters get "still
   happening, +N since last report" instead of being re-announced as new.
   Because these persist durably, novelty can look back over the whole history,
   not just a TTL window. This is the anti-alert-fatigue mechanism.
5. **LLM labeling**: fetch 3–5 exemplar tickets fresh from iTop (medoid +
   spread), prompt → cluster title, one-paragraph description, suggested
   action ("candidate Problem", "KB article", "training/self-service").
   Hierarchical: label clusters individually, then one digest-level LLM call
   over the labels — keeps every call small regardless of window size.

### D. LLM-only thematic digest — the MVP shortcut

For deployments below ~500 tickets/window, skip clustering entirely: feed
title + service of every window ticket into one (or few, chunked) LLM calls
— "group these into recurring themes with counts and example refs". Zero new
code beyond prompt + report plumbing, surprisingly strong results at small
scale, and it validates the *output side* (reports, UI, digest) before the
clustering investment. Config: `insights.miner: llm_digest | clustering`.

Recommended build order: **A + D first** (cheap, complete value loop), then
**B** (highest operational value, small delta over the retriever), then **C**
(replaces D when scale demands).

---

## 4. Outputs and actions (HITL)

**Reports** are the universal output: a row in `insights_report` (Postgres),
durable and queryable — `period`, `kind` and `env` are indexed columns so
"all weekly digests for org X over the last year" is one `SELECT`, and the
cluster/stat payload lives in a `jsonb` column. Retention is a housekeeping
job (`report_retention_days`, default 365, `NULL` = keep forever), not a
storage-level TTL. Payload shape:

```jsonc
{"id": "...", "kind": "weekly_digest | storm | problem_candidates",
 "period": {"from": "...", "to": "..."},
 "clusters": [{"label": "...", "summary": "...", "size": 14, "novelty": "recurring",
               "trend": "+5 vs prev period", "exemplar_refs": ["R-000123", "..."],
               "member_refs": ["..."], "suggested_action": "problem_candidate"}],
 "stats": {"anomalies": [{"dims": {"service": "Mail"}, "count": 42, "baseline": 18}]}}
```

Consumers:

- **Admin UI "Insights" page** (list + detail; ticket refs link into iTop).
- **Engineer widget**: `context` mentions active storms / cluster membership.
- **Notifications** (phase 2): digest by email/webhook-to-chat — via config,
  not hardcoded transport.

**Actions stay human-confirmed** (consistent with the HITL principle —
creating iTop objects is not reversible-enough for autonomy):

- *Create Problem from cluster*: button in the Insights UI →
  `POST /api/insights/reports/{id}/clusters/{n}/problem` → LLM-drafted
  title/description (from exemplars) → `core/create` Problem in iTop, link
  member tickets where the datamodel allows (stock iTop: the ticket→problem
  external key; exact attribute goes through a `ticket_mapping`-style config,
  verified in a spike). The response links to the created object; nothing is
  created without the click.
- *Storm internal notes* are the only autonomous write, and they are
  config-gated (`insights.storm_notes_enabled`, default off until trusted).

---

## 5. Access control

Reports aggregate across organizations — a new surface the per-ticket model
in vector-store §4 doesn't cover:

- MVP: Insights live **behind the admin token only** (admin UI), documented
  as potentially cross-org. No per-engineer exposure.
- When insights reach engineers (widget storm hints): the hint itself
  contains only counts + refs; refs are passed through the **existing
  layer-2 oracle** (`check-read`) in the proxy before display, so an
  engineer sees "part of a 12-ticket storm" but only the member refs they
  are allowed to open. Counts across forbidden orgs are a deliberate,
  documented leak of aggregate size only — if a deployment can't accept it,
  `insights.widget_hints_org_scoped: true` restricts storm membership to the
  engineer's allowed orgs at query time (layer-1 tags suffice: counts are
  not confidential content, they only need coarse correctness).
- Per-org digest generation (`insights.per_org: true`) is the clean answer
  for multi-tenant MSP setups — same jobs, org filter in the vector read,
  one report per org. Phase 2.

---

## 6. Pitfalls

1. **Threshold calibration** — cosine thresholds are embedding-model-specific
   (0.82 on one model ≈ 0.65 on another). Config per detector + a
   calibration helper: a CLI that samples known-duplicate and random ticket
   pairs and prints the similarity distributions, so a deployment sets
   thresholds from data, not folklore. Re-run after any embedding model
   change (index version bump already forces attention here).
2. **Alert fatigue** — novelty fingerprints (§3C) and storm-record extension
   (§3B) are core design, not polish; a pattern reported weekly with no
   change trains people to ignore the feature.
3. **Small-N noise** — `min_size`, caller-diversity check, and absolute-count
   floors on trends. Better to under-report at small scale.
4. **Embedding/model change invalidates baselines and fingerprints** —
   cluster fingerprints and storm centroids store the index version; jobs
   discard mismatched state and log a "recalibrating" report rather than
   comparing incomparable vectors.
5. **LLM label hallucination** — labels/summaries are generated from
   exemplar *content*, with refs attached; the UI always shows the refs so a
   human can verify in one click. Digest prompts forbid inventing counts
   (numbers come from code, LLM only narrates them).
6. **Window memory & runtime** — a 90-day window at big-deployment scale
   (say 50k tickets) is ~200 MB of float16 vectors in-process for the weekly
   job; acceptable, but stream in batches and cap (`max_window_docs`) with a
   loud warning instead of OOM.
7. **Job pile-up** — weekly clustering may take minutes (LLM labeling
   dominates); jobs runner runs jobs sequentially per lease, journals
   duration, and skips a tick rather than overlapping.
8. **Seasonality traps** — weekday-aligned baselines (§3A); holidays will
   still false-positive (document; a holiday calendar is not worth the
   complexity yet).
9. **Duplicate semantics** — storm detection deliberately includes tickets
   of *any* status in the window (a storm doesn't stop being one because two
   members were quick-resolved), while problem mining includes resolved ones
   (recurrence needs history). Filters are per-detector config, not shared.
10. **Report durability** — reports, baselines and cluster fingerprints live
    in Postgres, so a year of digests is a `SELECT`, not an export: the
    "Redis can't answer historical analytics" limitation that motivated the
    datastore fork is gone. Two consequences to own: (a) reports now
    accumulate — the retention job (§4) is what keeps the table bounded, not a
    storage TTL; (b) don't let durability tempt anyone into treating insights
    as source-of-truth — they remain a rebuildable derivative of the vector
    index (itself a derivative of iTop), and a re-run regenerates them.

---

## 7. Implementation stages

### Stage 0 — jobs framework
- [ ] `src/jobs/`: registry, interval + daily-at schedules, Postgres
      advisory-lock lease, `JobJournal` table (`GET /api/jobs/status`).
- [ ] Migrate the vector sweep onto it (behavior-neutral refactor; it already
      uses `pg_try_advisory_lock` from vector-store Stage 2).
- [ ] Alembic migration for `insights_*` tables (counts, baseline, cluster,
      storm, report) — reuses the Postgres foundation from vector-store
      Stage 0.
- [ ] Tests: schedule math, lease contention, journal entries
      (Testcontainers Postgres).

### Stage 1 — trends + LLM digest (approaches A + D)
- [ ] `InsightsConfig` (windows, thresholds, miner mode, retention;
      enabled=False).
- [ ] Trend stats job: OQL aggregates → `insights_counts`; weekday-aligned
      baselines via SQL (mean/stddev over trailing weeks); anomaly list.
- [ ] LLM digest job (`llm_digest` miner): chunked titles → themes; prompts
      under `prompts/insights/` with the standard placeholder registry.
- [ ] Report store + `GET /api/insights/reports[/{id}]` + admin UI Insights
      page (list/detail, refs linking to iTop).
- [ ] Tests: baseline math, anomaly flags, report round-trip, prompt
      validation.

### Stage 2 — storm detector (approach B)
- [ ] Reuse the `created_at timestamptz` chunk column (already in the
      vector-store schema, §2); sweep hook "ticket newly indexed → storm
      check".
- [ ] Storm records with centroid fingerprints; open/extend/close lifecycle;
      report entries.
- [ ] Config-gated internal notes on member tickets; widget `context` storm
      hint (+ oracle-filtered member refs).
- [ ] Tests: threshold behavior, storm extension vs re-alert, note gating.

### Stage 3 — problem-candidate mining (approach C)
- [ ] Leader clustering + novelty fingerprints; exemplar selection
      (medoid + spread); LLM labeling; caller-diversity scoring.
- [ ] "Create Problem" HITL action (spike first: exact stock-iTop attribute
      for ticket→problem linkage; config-mapped like ticket_mapping).
- [ ] Calibration CLI (similarity distribution sampler).
- [ ] Tests: clustering determinism on fixtures, novelty suppression,
      problem-creation payload.

### Stage 4 — quality & scale
- [ ] Optional HDBSCAN dependency group + config switch; comparison run mode
      (both miners on one window, diff report) to justify the dependency.
- [ ] Per-org digests; digest delivery (email/webhook transport config).
- [ ] KB gap analysis: clusters × FAQ index (needs vector-store Stage 5) →
      "missing article" suggestions feeding the KB-automation vision.

---

## 8. Open questions / assumptions

- **Scale envelope**: designed around 10³–10⁵ tickets per analysis window.
  Beyond that, clustering moves out-of-process (a dedicated worker) before
  Postgres itself becomes the constraint.
- **Ticket→Problem link attribute** in stock iTop needs the Stage 3 spike;
  customized datamodels configure it, same policy as `ticket_mapping`.
- **`start_date`/`created` OQL filters** for trends assume stock attributes;
  they go through the semantic mapping like everything else.
- **Digest delivery transport** (email vs chat webhook) is deferred until a
  real consumer exists; reports + admin UI are the MVP delivery.
- **Storm notes wording** must make clear it's a *hypothesis* ("possibly
  related"), not a diagnosis — the engineer decides; copy review before
  enabling by default.
