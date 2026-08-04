# Vector Store — Architecture & Implementation Plan

Semantic search over iTop objects (tickets first, KB/FAQ later) to power the
engineer console's "similar tickets", future KB matching, and RAG-style
suggestions. This is the *only* retrieval path for those features — the keyword
MVP [engineer-widget.md](engineer-widget.md) once planned as a stopgap was
dropped, so Stage 4 below is what unblocks them rather than what improves
them.

Status: **in progress.** The write path is built and shipped — the chunker,
the background sweep with cursors and reconciliation, the reindex CLI and
endpoint, the admin UI screen, and the pluggable `VectorSource` layer. What
remains is everything that *reads* the index: Stage 4 (retrieval into the
widget) is next, then access-control hardening and the first non-ticket
source.

> **Backend history.** This plan was written against **Postgres + `pgvector`**
> (see [stack-improvements.md §3](stack-improvements.md), decided
> 2026-07-07). That decision was reversed by
> [[ADR-001-vector-store-qdrant]] (dev-docs) and the relational
> implementation was removed from the codebase in `TASK-002-qdrant-backend`
> (dev-docs). §2 (schema) and §3 (indexing pipeline internals) below still
> describe the Postgres design as it was planned and briefly shipped — kept
> for the record, not current. **Current mechanics are
> `dev-docs/architecture/vector.md`**; the ground rules in §1, the access
> model in §4 and the retrieval flow in §5 were backend-agnostic to begin
> with and still hold. The store is Qdrant, gated behind `qdrant_url` +
> `vector.enabled` (was `database_url`); Redis still keeps operational state
> (ticket state, locks, config/prompt overrides, run journal, and now also
> the sweep's own cursors and lock — see the current doc).

---

## 1. Ground rules

**The index is a rebuildable derived cache, never a data store.** This is the
explicit carve-out from the "never duplicate iTop data locally" principle:

- Postgres holds **embeddings + ids + filter metadata only — no raw text**.
  Anything shown to a user or fed to an LLM is re-fetched fresh from iTop by
  id at query time. This kills three birds: staleness, deleted-object leaks,
  and "the DB becomes a shadow copy of the ticket database".
- The whole index must be reproducible from iTop with one backfill command.
  Losing the vector tables loses freshness, not data.
- iTop remains the only rights authority. The index provides *candidate ids*;
  what the user actually sees is always gated by an iTop-side check
  (see §4).

**Technology as built: Postgres + `pgvector`** — superseded by ADR-001, kept
here because it is what the code does today. HNSW index (`vector_cosine_ops`),
metadata filters as ordinary SQL `WHERE`/`JOIN`, ACL as a predicate rather than
hand-built tag strings. `halfvec` (16-bit) storage halves memory vs `vector`
(float32) with negligible recall loss at these dims. This is a new stateful
service for a project with zero relational storage today — that one-time cost
is accepted deliberately because it also unblocks pattern-analysis
(queryable history) and Langfuse tracing, both of which want a Postgres.
Access is async: `asyncpg` + SQLAlchemy (core/ORM), schema under Alembic.

**Embeddings via an OpenAI-compatible `/v1/embeddings` endpoint** — same
integration pattern as the LLM (LM Studio locally, LiteLLM/cloud in prod).
Separate config section: embedding models are distinct from chat models and
must be **multilingual** (tickets are ru/en mixed) — e.g. `bge-m3`,
`multilingual-e5-large`, or a cloud equivalent.

---

## 2. Document model: how to vectorize multi-field objects with logs

> **Historical.** The chunking rationale below still holds; the `Postgres
> schema` subsection is the Postgres design, superseded by Qdrant's
> collection layout (`dev-docs/architecture/vector.md`).

One iTop object ⇒ **several chunk rows**, grouped by semantic field, not one
concatenated blob. Rationale: fields change independently (re-embed only what
changed), logs grow append-only (embed only new entries), and retrieval
quality is better when a match pinpoints "the solution looked similar" vs
"somewhere in 4 pages of text".

### Chunk types for a ticket

| Chunk | Source (semantic fields) | Cardinality | Visibility |
|---|---|---|---|
| `profile` | title + service/subcategory names + request type | 1 | public |
| `body` | description (html→text, split by token budget) | 1..n | public |
| `solution` | solution (resolved/closed tickets) | 0..n | public |
| `log:public` | public_log, windows of N entries (no overlap) | 0..n | public |
| `log:private` | private_log, same windowing | 0..n | internal |

Chunk kinds are literally the keys of the per-class
`vector.classes[<class>].profile` config: the config is
the single source of truth, no mapping layer in between — which is why the
body kind is called `body`, matching the profile key, not `description`.

Log windowing: chunk boundaries are **stable by entry index** (entries
1–5 → chunk 0, 6–10 → chunk 1, …). Appending entries only creates/extends the
last chunk — earlier chunks' hashes don't move, so nothing is re-embedded.
Each log chunk text is prefixed with speaker roles ("caller: … / agent: …")
so the embedding captures the dialogue, not bare strings.

MVP scope: `profile` + `description` + `solution`. Log chunks are phase 2 —
logs are noisy and mostly help "find the ticket where we discussed X", a
distinct use case from "find similar problems".

### Postgres schema

```sql
CREATE TABLE vector_chunk (
    id            bigserial PRIMARY KEY,
    obj_class     text        NOT NULL,          -- UserRequest / Incident / FAQ …
    obj_id        bigint      NOT NULL,
    chunk_kind    text        NOT NULL,          -- profile / description / solution / log:public …
    chunk_n       int         NOT NULL,          -- ordinal within kind
    visibility    text        NOT NULL,          -- public / internal
    org_id        text,                          -- rights pre-filter (see §4); NULL = global
    status        text        NOT NULL,          -- query-time scoping (resolved-only etc.)
    filters       jsonb,                         -- source-defined pre-filter keys (see below)
    content_hash  text        NOT NULL,          -- sha256 of the chunk's cleaned source text
    embedding     halfvec(:dim) NOT NULL,        -- dim from config, baked into the table version
    created_at    timestamptz NOT NULL,          -- object creation — time-window KNN (storm detector)
    indexed_at    timestamptz NOT NULL default now(),
    UNIQUE (obj_class, obj_id, chunk_kind, chunk_n)
);

-- ANN index (cosine). Build params tuned in §6.
CREATE INDEX vector_chunk_emb_hnsw
    ON vector_chunk USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Metadata pre-filters ride alongside the ANN scan.
CREATE INDEX vector_chunk_filter
    ON vector_chunk (obj_class, status, visibility);
CREATE INDEX vector_chunk_org ON vector_chunk (org_id);
CREATE INDEX vector_chunk_obj ON vector_chunk (obj_class, obj_id);
-- jsonb_path_ops, not the default jsonb_ops: smaller/faster for `@>`
-- containment, the only operator this column is ever queried with.
CREATE INDEX vector_chunk_filters ON vector_chunk USING gin (filters jsonb_path_ops);
```

**Why `filters jsonb` instead of a typed `service_id` column:** the original
sketch had a plain `service_id text` here, intended as a future
retrieval-time filter/boost ("similar tickets in the same service"). It
shipped that way and was never actually read by `search()` — a ticket-only
column sitting unused in a table every future source would also write to.

The real reason it isn't a typed scalar column (an earlier draft of this
correction argued "no cheap `ALTER`" — wrong: adding a nullable column to an
already-created `vector_chunk_v{N}` is an ordinary, cheap Postgres `ALTER
TABLE ... ADD COLUMN`, unrelated to the model/dim versioning scheme) is
conceptual: `status` and `org_id` are each **one concept with a per-class
vocabulary** (every object has *a* lifecycle status, every object has *an*
owning org — the column identity is stable, only the value set varies by
class). A ticket's Service, a KB article's Category and a KnownError's CI
type are **not** the same concept wearing different vocabularies — they are
different concepts that happen to serve an analogous filtering *purpose*.
Forcing them into one typed column (`scope_id`) would silently conflate
unrelated id spaces under one name. `filters` sidesteps that: each source
picks its own key (`{"service_id": "5"}` for tickets, `{"category": "..."}`
for a future KB source), so values are namespaced by key instead of
collapsed into one column's identity. Cost: a GIN index is slower to write
and to query than a plain btree equality column — real, but at this
project's target scale (§6 pitfall 2: 10⁴–10⁶ chunks) it's dominated by the
embed call / iTop fetch / LLM rerank in the retrieval path, not the
deciding factor. Same discipline as the rest of the table applies: short
scalar values only, never free text — `filters` is a bag of pre-filter keys,
not an escape hatch for content.

No `text` column — deliberately (see §1). The one soft exception worth
allowing: a ~120-char `caption` (title excerpt) to render candidate lists
without an iTop round-trip; title is the least sensitive field, and the
authoritative rights check still happens before display. Decide in Stage 4;
default is "no text at all".

**Index/model versioning.** A `pgvector` column has a *fixed dimension*, so a
model or dimension change can't coexist in one table — vectors from different
models are not comparable anyway. The version lives in the **table name**:
`vector_chunk_v{N}` with the model fingerprint (model id + dim) recorded in a
`vector_index_meta` row. A model change means "create `v{N+1}` alongside,
backfill it, flip the `active_version` pointer, drop the old table" — never
mixed vectors in one table (§6, pitfall 1). Retrieval and the sweep both read
`active_version` from `vector_index_meta`; the sweep refuses to write into a
table whose recorded fingerprint ≠ current config.

### Extending to other classes

The chunking profile (which semantic fields become which chunk kinds) and
the relevance filter (which values of the class's relevance attribute keep
an object in the index; empty = index everything) are per-class config, same
philosophy as `ticket_mapping`:

```yaml
vector:
  classes:
    UserRequest: {index_values: [resolved, closed], profile: {profile: [title, service, subcategory], body: [description], solution: [solution]}}
    Incident:    {…}
    FAQ:         {index_values: [], profile: {profile: [title], body: [text]}}   # phase: KB matching
```

Tickets reuse `TicketRepository` semantics; new classes get their own thin
repository (as `CatalogRepository` does today). Processing code never sees
iTop attribute names. The source contract (`vector/source.py`): every
indexed class exposes a last-modification datetime and a relevance
attribute; the config lists only the relevance *values* — which attributes
those are is the source's own mapping concern (a KnownError source may key
relevance off any attribute, not necessarily a status).

This is an enforced seam, not a convention: a new class of content (KB
article, KnownError, …) is not "another entry in `vector.classes`" alone — it
needs a `VectorSource` implementation (`vector/source.py`) registered in
`vector_sources/registry.py`. A configured class with no registered source
logs a warning and is skipped.

---

## 3. Indexing pipeline: changes, appends, deletes

> **Historical.** The sweep/cursor/reconciliation *design* below is what
> shipped and is unchanged in spirit; the *code* it describes (`db.py`,
> `VectorIndex`, the Postgres advisory lock, `test/pg/`) was removed in
> `TASK-002-qdrant-backend`. Current mechanics, including the Redis lock that
> replaced the advisory lock: `dev-docs/architecture/vector.md`.

### Backbone: reconciliation sweep (not webhooks)

A periodic **sweep** is the primary mechanism; webhook events are a later
freshness optimization, not the foundation:

```
every sweep_interval (default 5 min):
  acquire sweep lease (pg_try_advisory_lock)       # safe with >1 replica
  for each indexed class:
    OQL: SELECT <class> WHERE last_update >= :cursor - overlap
         (paged, minimal output_fields)
    for each object:
      build chunks → sha256 each → compare with stored content_hash
      embed only changed/new chunks (batched)      # INSERT … ON CONFLICT upsert
      delete chunks that disappeared (e.g. description shortened → fewer chunks)
    cursor = max(last_update) seen   (persisted in vector_sync_state)
```

**Cursor granularity** (a deviation from the original sketch): iTop OQL
has no `ORDER BY`, pages arrive in internal order — a per-page
`max(last_update)` cursor would leave holes. The cursor therefore advances
once per *completed class pass*; a crashed pass just re-reads from the old
cursor, which the hash-guard makes cheap (iTop reads only, no re-embedding).
The overlap is derived (`2 × sweep_interval`), not a config knob, and cursor
values are always iTop's own `last_update` timestamps — no clock-skew
between our clock and iTop's.

Why sweep-first:

- **Backfill and incremental sync are the same code path** — initial indexing
  is just a sweep with `cursor = 0` (resumable: the cursor advances as pages
  complete).
- Self-healing: a missed event, a crashed run, or an assistant downtime never
  loses updates — the next sweep catches up.
- No iTop-side trigger explosion ("on every update of every ticket" triggers
  are exactly what the webhook loop-guards exist to fear).
- iTop's `last_update` moves on any modification **including case-log
  appends**, so one cheap OQL predicate covers field edits and new comments.

Idempotency: upserts are `INSERT … ON CONFLICT (obj_class, obj_id,
chunk_kind, chunk_n) DO UPDATE`, hash-guarded (skip embedding when the stored
`content_hash` matches), and the cursor overlaps backwards (default 2×
interval), so re-processing an object is a cheap no-op.

**Sweep cursor and lease live in Postgres now** (they no longer need to
borrow Redis): the cursor is a row in `vector_sync_state (class, cursor,
updated_at)`, and cross-replica exclusion is a session-level
`pg_try_advisory_lock` — one fewer moving part than the Redis SETNX lease.

**Deletes** are invisible to an incremental sweep — a deleted/archived object
just stops appearing. Two-layer answer:
- cheap: query-time re-fetch from iTop drops dead ids naturally (they simply
  don't come back), optionally deleting their rows on miss;
- correct: a **weekly full reconciliation** — walk indexed `obj_id`s in
  batches, `core/get` existence check, `DELETE` orphans. Also purges objects
  that left the indexable scope (e.g. status moved into an excluded set, org
  changed ⇒ re-tag).

### Where it runs

An asyncio background task started in the FastAPI lifespan — same process,
no new deployment unit. The Postgres advisory lock makes it safe if the
assistant is ever scaled to several replicas. Embedding requests are batched
(`embeddings.batch_size`, default 32) and throttled; iTop reads are paged
(`vector.sweep_page_size`, default 100) with minimal `output_fields`.

Observability: an `IndexJournal` — now a real **table**, queryable for
history (a bonus over the Redis `RunJournal` pattern) — records each sweep
(objects seen / chunks embedded / errors / duration), surfaced via
`GET /api/vector/status` and the admin UI. `POST /api/vector/reindex`
schedules a full rebuild; the same logic is exposed as a CLI
(`python -m vector.reindex`) for cold-start backfill.

### New components

```
assistant/src/vector/            # source-agnostic infrastructure
├── db.py         # async engine/session factory (asyncpg + SQLAlchemy), pgvector types
├── models.py     # SQLAlchemy models: vector_chunk_v{N}, vector_sync_state, vector_index_meta, index_journal
├── embedder.py   # EmbeddingsClient: OpenAI-compatible /v1/embeddings, batching
├── index.py      # VectorIndex: table create/upsert/delete/KNN over SQLAlchemy — the single SQL seam
├── chunker.py    # object → [(chunk_kind, n, text, visibility, hash)] — no domain imports
├── source.py     # VectorSource protocol + VectorRecord — the sweep's only contract with a source
├── indexer.py    # sweep loop, cursor, advisory lock, reconciliation, backfill CLI — drives VectorSource, not iTop
├── retriever.py  # query → embed → filtered KNN → ids → fresh fetch from the owning source
└── router.py     # /api/vector/status, /api/vector/reindex (admin-token auth)

assistant/src/vector_sources/    # one module per content source
├── registry.py   # build_vector_sources() — same pattern as pipelines/registry.py
├── tickets.py    # TicketVectorSource — wraps TicketRepository + CatalogRepository
# later: kb_articles.py, known_errors.py, ...
```

`index.py` is the **only** place that knows SQL/pgvector — chunker, indexer and
retriever speak to it through a narrow `VectorIndex` interface, so the storage
layer stays swappable and unit-testable in isolation.

Config: `EmbeddingsConfig` (runtime-editable section like `llm`: base_url,
model, api_key, dimension, batch_size) + `VectorConfig` (enabled=False by
default, per-class `classes` dict (index_values + profile), sweep_interval,
page size, max_chunk_tokens,
log_entries_per_chunk) + a bootstrap `database_url` (asyncpg
DSN, env-only like `redis_url` — connection settings, not runtime-editable).
Wired through `AppDeps`. There is no alternative retrieval backend to switch
to: a consumer whose index is absent, empty or unreachable reports the feature
as unavailable with the reason (see the widget plan §3), which is why none of
the settings above has a per-consumer mirror.

---

## 4. Access control: mapping iTop rights onto vector search

The problem: iTop rights (profiles × allowed organizations × portal rules ×
possible custom `UserRights` addons) are evaluated inside iTop's PHP — we
cannot faithfully re-implement them in Python, and trying would rot silently.

**Three-layer model — filter coarsely early, verify exactly late, keep the
index content-free:**

### Layer 1 — metadata pre-filter in SQL (coarse, fast)

Every chunk row carries `org_id` and `visibility`. At query time the retriever
resolves the requesting user's scope and folds it into the same `WHERE` that
runs the ANN scan — no hand-built tag strings, just parameterized predicates:

- **allowed orgs**: fetched from iTop (`User.allowedorg_list` via REST;
  empty list = all orgs), cached per login with a short TTL (5 min).
  KNN filter: `AND org_id = ANY(:allowed_orgs)` (omitted when unrestricted).
- **audience**: engineer widget queries pass
  `AND visibility IN ('public','internal')`; any future portal/caller-facing
  feature passes `visibility = 'public'` only.

This mirrors the dominant stock-iTop rights dimension (org scoping) and cuts
the candidate set before anything touches iTop. It is an *optimization and a
first fence*, *not* the security boundary.

Under Qdrant (ADR-001) the same predicates become collection filters applied
*during* the graph walk rather than a `WHERE` over what the index already
returned. The security boundary does not move — it was layer 2 all along; what
changes is that a restrictive pre-filter now costs recall instead of silently
costing results. The rule the filter must obey is unchanged and worth stating
outright: it is built **deliberately over-permissive** — it may let through
more than the user can see, it may never drop something they can.

### Layer 2 — authoritative check in iTop before exposure (exact)

Before candidate content reaches the user **or an LLM prompt**, the id list
is validated by iTop itself. The `knowitop-ai-widget` extension (see the
widget plan) grows a **rights-oracle route**:

```
POST ajax.knowitop-ai-widget.php?route=check-read
     {class, ids: [..]}            # executed in the engineer's own session
  → {allowed_ids: [..]}            # UserRights::IsObjectAllowedRead per id
```

Flow for widget "similar tickets" (vector mode):

```
browser → proxy(similar) → assistant: KNN (layer-1 filtered) → candidate ids
        ← assistant returns ids + scores (no content)
proxy: filters ids through UserRights in the live session   # layer 2
proxy → assistant: finalize(ids=allowed)                    # rerank + excerpts
        → assistant fetches those tickets fresh from iTop, builds response
        ← rendered result to the browser
```

Two proxy legs instead of one, but every piece of content the LLM sees or the
user reads has passed a real iTop rights check **in the requesting user's
session** — custom rights extensions included, with zero rights logic
re-implemented on our side. (Implementation detail: `finalize` may be an
internal parameter of the same `similar` route rather than a separate public
endpoint.) Note that moving to SQL does **not** move the security boundary —
layer 2 stays exactly as it was; the only change is that layer 1 is now a
predicate instead of an `FT.*` tag string.

For non-interactive callers (future background RAG jobs, cron reports) where
there is no user session, the extension can expose the same oracle with
`user_login` + `UserRights::Impersonate()` under the widget token — same
authority, assistant-initiated. Keep it out of MVP until a real consumer
exists.

### Layer 3 — the index itself leaks (almost) nothing

Because rows store no text, a compromised or over-permissive query can leak
at most: object ids, class, org tag, status, and similarity structure.
Unpleasant, not catastrophic — and why §2 defaults to "no `caption` column"
until there's a strong UX reason. Raw vectors are never returned by any API
(embedding-inversion attacks reconstruct text from vectors; treat vectors as
sensitive), and `/api/vector/*` is admin-token only.

### Honest limitations (document, don't hide)

- The layer-1 org cache means a **revoked org membership lingers up to the
  TTL** in *candidate selection*; layer 2 still blocks actual exposure
  immediately, so the lag affects performance, not confidentiality.
- Rights dimensions beyond org + visibility (e.g. team-scoped funnels,
  per-portal power rules) are *not* pre-filtered — they are only enforced at
  layer 2. Consequence: KNN top-K may be thinned by the oracle; the retriever
  over-fetches (K×3) and, if everything is filtered out, returns "nothing
  visible" rather than digging unboundedly.
- That over-fetch compensates for **layer-2 thinning only**. It is not a
  remedy for a store that post-filters: there the shortfall grows with
  selectivity and no fixed multiplier covers it (ADR-001). Track the share
  discarded at layer 2 as a metric — it is the one number that says whether
  the coarse pre-filter still resembles the real rights model.
- If a custom datamodel scopes rights by something other than org, the
  layer-1 predicate set is extensible via config (`vector.filter_columns`,
  materialized as extra `vector_chunk` columns), but that is tuning, not
  correctness — correctness always comes from layer 2.

---

## 5. Retrieval flow (widget "similar tickets", vector mode)

1. Build the query text from the current ticket (`profile` + `description`
   chunks' source text), embed it (1 call).
2. Filtered KNN in one SQL statement — ANN order-by plus the layer-1
   predicates and scoping, over-fetching K×3:

   ```sql
   SELECT obj_id, max(1 - (embedding <=> :q)) AS score
   FROM   vector_chunk_v{active}
   WHERE  obj_class = ANY(:classes)
     AND  status IN ('resolved','closed')
     AND  visibility IN ('public','internal')
     AND  (:allowed_orgs IS NULL OR org_id = ANY(:allowed_orgs))
     AND  obj_id <> :current_id
   GROUP  BY obj_id                      -- aggregate chunks → objects
   ORDER  BY score DESC
   LIMIT  :k3;
   ```

   `GROUP BY obj_id` with `max(score)` does the chunk→object aggregation in
   the database (a ticket matching on both description and solution shouldn't
   count twice; taking max, not sum, avoids rewarding verbosity). This is the
   pgvector win over hand-rolled per-object dedup: it's one query.
3. Layer-2 oracle check → allowed ids.
4. Fresh `core/get` from iTop for the top allowed objects (title, status,
   solution) → LLM rerank with reasons (reuse the widget's rerank prompt) →
   top-N response.

Note the ANN + `WHERE` + `GROUP BY` interaction: pgvector's HNSW is
approximate, so aggressive pre-filters can thin the `ef_search` frontier —
tune `ef_search` (query-time) up and over-fetch K×3 to keep recall healthy
(§6). Latency budget: embed ~50–200 ms, KNN ~ms–low-tens, iTop fetch
~100–300 ms, rerank is the dominant LLM call. That total is what dictates the
consumer UX: on demand, with a spinner, never on page load. Hybrid retrieval (full-text +
vector fusion) is a natural later upgrade — Postgres does BM25-ish ranking via
`tsvector`/`ts_rank` and pgvector in the same query — but it requires storing
text in the index, so it conflicts with §1's "no text" stance — evaluate only
with anonymization or accept the trade-off consciously.

---

## 6. Pitfalls & mitigations

1. **Embedding model or dimension change** ⇒ all vectors invalid, and a
   `pgvector` column dim is fixed. Versioned table (`vector_chunk_v{N}`) +
   background rebuild + `active_version` pointer swap (§2). The sweep refuses
   to write into a table whose recorded model fingerprint ≠ current config,
   and the status endpoint screams "rebuild required" instead of silently
   mixing.
2. **Memory / storage sizing.** float32 @1024 dims = 4 KB/vector; `halfvec`
   halves it to ~2 KB. ~4 chunks/ticket ⇒ 100k tickets ≈ 0.8 GB of vectors +
   HNSW graph overhead (roughly comparable again). Lives on disk with a hot
   set in `shared_buffers` — unlike Redis it need not fit fully in RAM, which
   removes the "vectors compete with operational state for one process's
   memory" problem entirely. Status endpoint reports row count and relation
   size (`pg_total_relation_size`). At ~1M+ objects, revisit partitioning /
   `ivfflat` vs `hnsw` trade-offs.
3. **HTML & noise in fields** — strip via the existing `html_to_markdown`
   before hashing/embedding (hash the *cleaned* text, so cosmetic HTML churn
   doesn't trigger re-embeds). Signatures/quoted-reply trimming in log
   entries is a quality lever for phase-2 log chunks.
4. **Token limits of embedding models** (often 512) — `max_chunk_tokens`
   budget with sentence-boundary splitting; oversize descriptions become
   multiple chunks, not truncations.
5. **Sweep load on iTop** — paged reads, minimal `output_fields`, throttle
   between pages; backfill is resumable so it can run through quiet hours.
   The REST user needs bulk-read only (no new privileges).
6. **`last_update` skew / lost updates** — cursor overlap + hash-guarded
   idempotent `ON CONFLICT` upserts make double-processing free and
   missed-window loss near-impossible; weekly reconciliation is the backstop.
7. **Deleted/archived objects** — invisible to incremental sweeps; handled by
   query-time re-fetch (natural drop) + weekly orphan `DELETE` (§3).
8. **Multiple replicas double-sweeping** — `pg_try_advisory_lock` around the
   sweep (replaces the Redis SETNX lease).
9. **HNSW recall vs filters** — heavy pre-filtering can starve the ANN
   frontier. Tune `hnsw.ef_search` per query, over-fetch K×3, and add btree
   indexes on the filter columns so the planner can combine them. Validate
   recall on a labelled sample when tuning `m` / `ef_construction`.
10. **Migrations discipline** — schema changes go through Alembic; the
    versioned-table swap (pitfall 1) is a data migration, scripted and
    reversible, not a manual `psql` session. Test migrations in CI against a
    Testcontainers Postgres.
11. **Durability & backups** — Postgres WAL/backups make the index durable,
    but still treat it as rebuildable: after a wipe the status endpoint shows
    `docs=0` and the next backfill restores everything. Don't let "it's in
    Postgres now" tempt anyone into treating vectors as source-of-truth data.
12. **PII/security posture** — vectors derive from ticket text and are
    invertible in principle: Postgres must be treated as sensitive as the iTop
    DB (network isolation, `scram-sha-256` auth, TLS in prod; compose keeps it
    on the internal network). No raw-vector egress via API.
13. **Multilingual quality** — pick a multilingual embedding model
    explicitly; add a "known-good models" note in docs. With no keyword
    fallback to hide behind, a bad embedding model is directly visible as bad
    results: `test-embeddings` proves the endpoint answers and the dimension
    matches, but not that the model is any good at ru/en tickets. Validate on
    a labelled sample before trusting it.
14. **Cold-start cost** — backfilling 100k tickets on a local embedding model
    takes hours: resumable cursor and progress in the status endpoint. There
    is no keyword mode to serve results meanwhile, so consumers must say "the
    index is still building" rather than return thin results as if complete.
15. **Env/key hygiene** — one Postgres database per deployment; staging/prod
    isolation is a separate database (or schema) per environment, not a
    discriminator column. (An earlier draft had an `env` column filtering
    every query — dropped as speculative complexity.)

---

## 7. Remaining stages

**Stage order revised on 2026-08-04.** Replacing the backend now comes before
the read path: a retriever written against SQL predicates and `GROUP BY obj_id`
would have to be rewritten against collection filters and Qdrant's built-in
grouping. Stages 4–6 below survive as a list of work, but they are executed on
top of Qdrant, and the two carry-overs listed next apply to whichever store is
underneath.

The write path (§2–§3) is built; what follows is the read path. Two carry-overs
worth knowing before starting:

- **`filters` has no readers yet.** Ticket chunks are written with
  `{"service_id": ...}`, but nothing filters on it — wiring it up is Stage 4's
  job, deliberately left until there was a query to serve.
- **Per-source config namespacing is deferred.** `VectorConfig` is flat
  (one `enabled`/`classes`/`profiles` set shared by all sources), which is fine
  with exactly one source. Revisit when a second source needs its own settings.

### Stage 4 — retrieval
Now on the critical path: the engineer console's similar-tickets feature is
blocked on this stage and has no reduced mode to ship first.
- [ ] `retriever.py`: embed → filtered KNN (`GROUP BY obj_id`, max score) →
      fresh fetch from the owning source → rerank prompt.
- [ ] Layer-1 predicates wired (`status`, `visibility`, org when available).
- [ ] A typed "why is there nothing" answer — index absent / disabled / empty /
      unreachable are distinct states a consumer must be able to show the user,
      not one empty list.
- [ ] Tests: aggregation, filter composition, and each unavailable state.

### Stage 5 — access control hardening
- [ ] Allowed-orgs resolver (`User.allowedorg_list` via REST) + per-login TTL
      cache; org predicate in all retrievals.
- [ ] Rights-oracle route (`check-read`) in the iTop extension; two-leg
      widget flow (candidates → session check → finalize).
- [ ] Over-fetch (K×3) + "nothing visible" degradation; audit log of filtered
      ids (`X-Itop-User` already flows through).
- [ ] Tests: oracle-filtered flow with mocked proxy responses; cache TTL.

### Stage 6 — beyond tickets
- [ ] First non-ticket `VectorSource` (KB articles: `KbArticleVectorSource` →
      widget "KB suggestions"; or KnownError: symptom-based workaround
      matching for Incidents) — proves the source seam with a second
      implementer instead of just the one.
- [ ] With that second source in hand, revisit per-source config namespacing
      (`vector.sources.<name>.*`).
- [ ] Phase-2 chunk kinds: `log:public` / `log:private` windows.
- [ ] Hybrid retrieval is no longer an open evaluation: ADR-007 settles it as
      a sparse component merged with RRF, with the slot reserved in the
      collection schema from the start and switched on later. The `tsvector`
      variant dies with the Postgres backend.

---

## 8. Open questions / assumptions

- **Embedding endpoint availability**: assumes the deployment can serve an
  OpenAI-compatible `/v1/embeddings` (LM Studio, LiteLLM, cloud all can). A
  chat-LLM-only deployment does not get semantic search at all — with the
  keyword fallback dropped, that is a feature the customer does not have
  rather than a degraded one.
- **`allowedorg_list` via REST**: needs a verification spike — the `User`
  class link set must be readable by the service account; if not, the
  extension oracle can also serve the org list (it has full `UserRights`).
- **iTop `last_update` on log append**: assumed true (log append modifies the
  ticket). The sweep is implemented on this assumption; the live spike (append
  to a ticket's public log in iTop → the ticket shows up in the next sweep)
  is still pending. If false for some class, fall back to also matching on
  log `lastentry` dates.
- ~~**Scale envelope**~~ — *settled by ADR-001.* The dedicated vector store is
  the answer, chosen for filtering behaviour rather than for scale.
- ~~**Postgres operational ownership**~~ — *settled by ADR-001.* Postgres
  leaves the stack; the ops cost it would have introduced is not paid.
- **Embedding dimensionality** (ADR-006): MRL truncation to 512 or 256 without
  re-embedding. At a million chunks that is 4 GB versus 250 MB — the
  difference between "brute force is impossible" and "brute force is a working
  fallback", which is what makes a store that chooses between graph walk and
  full scan worth having in the first place.
- **Analytics storage is now open.** Postgres was also going to serve
  pattern-analysis (`GROUP BY`, `date_trunc`); with it gone, that plan has no
  assumed store. Deliberately deferred to the pattern-analysis work rather than
  keeping a database installed for a subsystem that does not exist yet.
- **Scope**: engineer/backoffice consumers only. Any portal- or caller-facing
  retrieval must re-run this design's §4 with the portal rights model in
  scope (`visibility = 'public'` alone is not enough — callers see only
  *their* tickets).
