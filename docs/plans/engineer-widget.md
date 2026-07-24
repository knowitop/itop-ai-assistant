# Engineer Widget — Architecture & Implementation Plan

Contextual AI sidebar inside the iTop backoffice UI: on a ticket details page
the engineer gets AI help — what the assistant already did with the ticket,
a summary of the conversation, similar past tickets with their resolutions,
and a draft reply to the user.

Status: **plan** (nothing implemented yet).

---

## 1. Architecture Overview

```
┌─ Engineer's browser ────────────────────────────────┐
│  iTop backoffice page (UserRequest / Incident)      │
│  ┌───────────────────────────┐  ┌────────────────┐  │
│  │ iTop page DOM             │  │ AI sidebar     │  │
│  │ .object-details           │  │ (widget.js)    │  │
│  │   data-object-class       │◄─┤ reads class/id │  │
│  │   data-object-id          │  │ from DOM       │  │
│  └───────────────────────────┘  └───────┬────────┘  │
└─────────────────────────────────────────┼───────────┘
                          same-origin fetch (iTop session cookie)
                                          ▼
┌─ iTop server ───────────────────────────────────────┐
│  extension: knowitop-ai-widget                      │
│   • injects widget.js/.css on backoffice pages      │
│   • ajax proxy: session check + route whitelist     │
│     + adds X-Auth-Token (widget_token) server-side  │
│     + adds X-Itop-User (engineer login) for audit   │
└─────────────────────────────────────────┬───────────┘
                              HTTP (server-to-server)
                                          ▼
┌─ assistant (FastAPI) ───────────────────────────────┐
│  /api/widget/*  — new "widget" module               │
│   • TicketRepository / CatalogRepository (iTop API) │
│   • LLM calls (summary, rerank, draft)              │
│   • Redis: response cache + config + prompts        │
└─────────────────────────────────────────────────────┘
```

### Key decision: PHP proxy, not direct browser→assistant calls

The widget JS talks only to its own iTop origin; the iTop extension proxies
whitelisted routes to the assistant. Rationale:

- **No secret in the browser.** `widget_token` lives in the iTop config file
  and is attached server-side.
- **Engineer authn for free.** The proxy enforces the iTop backoffice session
  (`LoginWebPage::DoLogin()`); the assistant trusts the proxy and receives the
  engineer's login in a header for audit/logging.
- **No CORS, no mixed-content.** The assistant does not need to be reachable
  from engineers' browsers at all — only iTop server → assistant (same
  topology as webhooks, just reversed).

Trade-off: no easy SSE streaming through the proxy. MVP endpoints are
synchronous JSON with a spinner in the UI; streaming is a later phase (either
chunked proxy or an optional direct-CORS mode).

### Embedding into the iTop UI

iTop 3.x extension interfaces (module `knowitop-ai-widget`):

- `iBackofficeLinkedScriptsExtension::GetLinkedScriptsAbsUrls()` — injects
  `widget.js` on all backoffice pages.
- `iBackofficeLinkedStylesheetsExtension::GetLinkedStylesheetsAbsUrls()` —
  injects `widget.css`.

`widget.js` self-activates only on object details pages: iTop 3.x renders
`div.object-details[data-object-class][data-object-id]` — if the class is in
the enabled list (fetched from the proxy `config` route), render the sidebar;
otherwise do nothing. No server-side page hooks (`iApplicationUIExtension`)
needed for the MVP — pure JS keeps the extension trivial and iTop-version
tolerant.

UI shape: a fixed toggle button at the right edge of ticket pages; clicking it
slides out a collapsible panel with sections. Vanilla JS + CSS, one file each,
no build step (same simplicity policy as `ui/`).

### iTop extension layout (new top-level dir)

```
itop-extension/knowitop-ai-widget/
├── module.knowitop-ai-widget.php    # module declaration (datamodel-less)
├── main.knowitop-ai-widget.php      # the two *LinkedScripts/Stylesheets classes
├── ajax.knowitop-ai-widget.php      # session-guarded proxy to the assistant
└── asset/
    ├── widget.js                    # all UI logic, vanilla JS
    └── widget.css
```

iTop-side configuration (`config-itop.php` module_settings):

```php
'knowitop-ai-widget' => array(
    'assistant_url' => 'http://assistant:8000',   // server-to-server URL
    'widget_token'  => '<same value as assistant security.widget_token>',
),
```

Proxy contract (`ajax.knowitop-ai-widget.php`):

- `LoginWebPage::DoLogin()` — backoffice session required.
- `?route=` whitelist: `config`, `context`, `summary`, `similar`,
  `draft-reply`; anything else → 404. `class`/`id` passed through as-is —
  the assistant re-fetches the ticket from iTop anyway (system of record),
  so a tampered id leaks nothing the engineer could not open in iTop itself.
  (Optional hardening later: `UserRights::IsObjectAllowedRead()` per id.)
- Adds headers: `X-Auth-Token: widget_token`,
  `X-Itop-User: UserRights::GetUserLogin()`.
- Forwards JSON body for POST routes; returns assistant's status/body
  verbatim; maps network errors to 502 with a short JSON error.

Deployment note: in the dev compose, mount `itop-extension/` into the iTop
container's `extensions/` dir and re-run the iTop setup once to register the
module (iTop requires setup to pick up new modules).

---

## 2. Backend: new "widget" module

Not a webhook pipeline — a synchronous query API. Lives in `src/widget/`
(peer of `src/admin/`), but registers a `ModuleInfo` (with an empty route
map) so its config and prompts show up in the existing admin UI machinery.

```
assistant/src/widget/
├── __init__.py
├── router.py      # FastAPI router /api/widget/*, X-Auth-Token check
├── service.py     # WidgetService: ticket fetch, LLM calls, caching
└── prompts.py     # WidgetPrompts + PROMPT_VARIABLES registry (mirrors enrichment)
assistant/prompts/widget/
├── summary_system.md / summary_human.md
├── similar_keywords_system.md / similar_keywords_human.md
├── similar_rerank_system.md / similar_rerank_human.md
└── draft_reply_system.md / draft_reply_human.md
```

### Endpoints

All under `/api/widget`, auth via `X-Auth-Token` == `security.widget_token`
(new secret field; `None` = auth disabled + startup warning, same policy as
webhook_token). Responses are plain JSON.

| Endpoint | LLM | Purpose |
|---|---|---|
| `GET /api/widget/config` | no | Bootstrap for widget.js: enabled flag, enabled classes, feature flags. |
| `GET /api/widget/tickets/{class}/{id}/context` | no | Ticket snapshot (ref, title, status) + AI activity: `TicketState` (rounds, ai_done) + recent `RunJournal` entries for this ticket. Rendered as the "AI activity" section. |
| `GET /api/widget/tickets/{class}/{id}/summary` | yes | Summary of description + public log. Cached. |
| `GET /api/widget/tickets/{class}/{id}/similar` | yes | Similar resolved/closed tickets with solution excerpts. Cached. |
| `POST /api/widget/tickets/{class}/{id}/draft-reply` | yes | Body `{"instruction": "optional engineer hint"}` → draft public reply text. Not cached. |

Response sketches:

```jsonc
// context
{"ticket": {"ref": "R-000123", "title": "...", "status": "new"},
 "ai": {"ai_done": true, "rounds": 1, "classify_rounds": 0,
        "runs": [{"run_id": "...", "status": "completed", "steps": [...]}]}}

// summary
{"summary": "markdown text", "cached": false}

// similar
{"items": [{"obj_class": "UserRequest", "id": "42", "ref": "R-000042",
            "title": "...", "solution_excerpt": "...", "reason": "same error code"}],
 "cached": true}
// URLs are built client-side (same origin): /pages/UI.php?operation=details&class=..&id=..

// draft-reply
{"reply": "text the engineer can paste into the public log"}
```

The `X-Itop-User` header is logged with every request (audit trail: which
engineer asked for what).

### Similar tickets — MVP retrieval (no embeddings)

Two-step, LLM-assisted, everything read fresh from iTop:

1. **Keyword extraction** (`similar_keywords` prompt): LLM pulls 3–5 search
   terms from the ticket title/description.
2. **Candidate search**: OQL over resolved/closed tickets of the same class —
   `title LIKE '%kw%'` per keyword (OR-joined), optional same-service boost,
   `limit` = `similar_max_candidates` (default 30). OQL template is config
   (`widget.similar_oql`) with the same `:this->field` binding as the
   classify OQLs; keyword interpolation escapes quotes.
3. **Re-rank** (`similar_rerank` prompt): LLM gets candidate titles +
   solution excerpts and the current ticket, returns top-N (default 5) with a
   one-line relevance reason. Non-parseable LLM output degrades to the raw
   OQL order.

`TicketRepository` gains: semantic fields `solution` (+ mapping entry in
`TicketFieldMap`, default `"solution"`) and a
`search(oql, bind_ticket, limit)` read method returning lightweight `Ticket`
objects. Nodes/services still never see iTop attribute names.

**Phase 2+ (explicitly out of MVP): embeddings.** Redis 8 ships vector search
in core, so a background indexer (ticket ref + embedding only, rebuildable,
TTL-managed) is feasible without new infrastructure — but it deviates from
the "never store iTop data locally" principle and needs its own design pass
(indexing triggers, backfill, redaction). Keyword+rerank first; measure
quality before paying that complexity.

### Caching & cost control

- Redis cache per feature: `widget:cache:{feature}:{class}:{id}` →
  `{content_hash, payload, ts}`; `content_hash` covers title, description and
  public-log length, so a new comment invalidates naturally. TTL
  `widget.cache_ttl_minutes` (default 60).
- All LLM features are **on demand** (engineer clicks a button); nothing is
  computed just because a page opened. `context` and `config` are free reads.
- UI disables buttons while a request is in flight; a Redis per-ticket lock
  against concurrent duplicate LLM calls is a later hardening step.

### Config section (`config.py`)

```python
class WidgetConfig(BaseModel):
    enabled: bool = True
    classes: list[str] = ["UserRequest", "Incident"]
    summary_enabled: bool = True
    similar_enabled: bool = True
    draft_reply_enabled: bool = True
    similar_statuses: list[str] = ["resolved", "closed"]
    similar_max_candidates: int = 30
    similar_top_n: int = 5
    similar_oql: str = _WIDGET_SIMILAR_OQL
    # Per-feature model overrides; None falls back to the global llm.model
    summary_model: str | None = None
    similar_model: str | None = None
    draft_model: str | None = None
    cache_ttl_minutes: int = 60
```

Plus `SecurityConfig.widget_token: str | None` (added to `SECRET_FIELDS`;
editable through the existing `/api/setup/security` endpoint and the
Connections UI with zero extra backend work).

Runtime-editable like enrichment: `widget.*` via `/api/config/widget`,
prompts via `/api/prompts/widget` — both come free from the `ModuleInfo`
registration.

### Human-in-the-loop guarantees

The widget is **read-only towards iTop**. It never posts to logs or updates
fields. The draft reply is inserted client-side into the engineer's case-log
textarea (or copied to clipboard) — the engineer reviews, edits, and posts it
under their own name. This keeps the existing "AI acts as a named user" and
HITL principles intact: widget output is advice, not action.

---

## 3. Feature priorities (Service Desk rationale)

MVP order — value vs. effort for a service desk engineer:

1. **AI activity panel** (`context`) — zero LLM cost, instant trust-builder:
   shows what the assistant already did (classification, questions asked,
   enrichment note) without digging through logs.
2. **Similar past tickets + how they were resolved** — the single biggest
   time-to-resolution lever: reuses the team's institutional memory that
   today lives only in closed tickets.
3. **Conversation summary** — cheap and valuable on long threads,
   reassignments, and escalations ("catch me up in 10 seconds").
4. **Draft reply** — speeds up the most repetitive part of the job; safe
   because the engineer posts it themselves.

Later phases (in rough order):

5. **Suggested resolution / next actions** — synthesized from similar tickets'
   solutions; needs quality similar-search first.
6. **KB article matching** (iTop `FAQ` class) — same retrieval machinery,
   different corpus.
7. **KB draft from a resolved ticket** — "this ticket looks KB-worthy" +
   generated draft (feeds the knowledge-base-automation vision).
8. **Embeddings retrieval** (Redis vector search) — replaces keyword search
   when its quality ceiling is hit.
9. **Feedback buttons** (👍/👎 per suggestion → journal) — data for prompt
   tuning.
10. **Streaming** responses for summary/draft (chunked proxy or direct mode).

---

## 4. Implementation stages

### Stage 0 — end-to-end spike (embedding proof)
- [ ] `itop-extension/knowitop-ai-widget/`: module + main + ajax + empty panel
      JS/CSS; `config` route proxied to assistant `/health`.
- [ ] Compose: mount `itop-extension/` into the iTop container, document the
      one-time setup re-run.
- [ ] Verify: panel appears on UserRequest/Incident details only; proxy
      reaches the assistant; unauthenticated browser hit on ajax page is
      rejected.

Exit criterion: sidebar renders inside iTop and gets JSON from the assistant
through the proxy. Everything after this is incremental.

### Stage 1 — backend module skeleton
- [ ] `SecurityConfig.widget_token`; `WidgetConfig` in `config.py`.
- [ ] `src/widget/router.py`: auth dependency, `GET /config`,
      `GET /tickets/{class}/{id}/context` (TicketState + RunJournal reads;
      journal needs a "runs by ticket ref" lookup if not already there).
- [ ] Register `ModuleInfo(name="widget", routes={})` in `build_registry`.
- [ ] Tests: `test_widget_api.py` — auth on/off, config, context happy path
      + unknown class.

### Stage 2 — summary
- [ ] `prompts/widget/summary_*.md`, `src/widget/prompts.py` with placeholder
      registry; startup validation like enrichment.
- [ ] `WidgetService.summary()`: fetch ticket, html→markdown (reuse
      `nodes/utils.py` helpers — consider promoting them out of enrichment),
      LLM call, `strip_thinking`, Redis cache.
- [ ] UI: Summary section with "Generate" button, loading state, error state.
- [ ] Tests: caching behavior (hash invalidation), prompt validation.

### Stage 3 — similar tickets
- [ ] `TicketFieldMap.solution` + `TicketRepository.search()`.
- [ ] Keyword extraction + OQL candidates + rerank in `WidgetService`;
      quote-escaping for keywords; degrade to raw order on parse failure.
- [ ] UI: Similar section — ref/title links (same-origin URL), solution
      excerpt, relevance reason.
- [ ] Tests: OQL building/escaping, rerank parse fallback, empty results.

### Stage 4 — draft reply
- [ ] `draft_reply` prompt + endpoint (instruction passthrough, optional
      inclusion of top similar solutions in context).
- [ ] UI: instruction input, "Generate", then "Copy" + "Insert into reply"
      (fills the case-log textarea — best-effort DOM integration, Copy is the
      guaranteed path).
- [ ] Tests: endpoint + prompt placeholders.

### Stage 5 — polish & docs
- [ ] Admin UI: widget section appears in Modules/Prompts (should be mostly
      free via ModuleInfo; verify and fix gaps).
- [ ] Setup docs: `docs/widget.md` — extension install, config file snippet,
      token wiring; README feature mention.
- [ ] Extension i18n pass (strings table in widget.js is enough; iTop Dict
      files only if we later render server-side).
- [ ] Hardening: per-ticket in-flight lock; proxy timeout/502 mapping review.

---

## 5. Open questions / assumptions

- **iTop version**: assumes 3.x backoffice (`data-object-class` DOM markers,
  `iBackofficeLinkedScriptsExtension` — 3.0+). 2.7 is out of scope.
- **Backoffice only**: no end-user portal widget in this plan.
- **Widget token distribution is manual** (iTop config file). Provisioning
  via `POST /provision-itop` can't write iTop config files — acceptable,
  documented step.
- **Journal lookup by ticket**: `context` needs runs filtered by ticket ref;
  if `RunJournal` only supports listing, add a secondary index
  (`runs:by_ref:{ref}`) in Stage 1.
- **`solution` field on Incident**: stock iTop has it on both UserRequest and
  Incident; customized datamodels handle it via `ticket_mapping`
  class_overrides as usual.

## References

- iTop 3.0 extension interfaces overview:
  https://www.itophub.io/wiki/page?id=3_0_0%3Acustomization%3Aapi%3Aextensions%3Astart
- `iBackofficeLinkedScriptsExtension` docs (linked from the overview page).
