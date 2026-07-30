# Engineer Widget — Architecture & Implementation Plan

An AI console the engineer talks to while working a ticket: what the assistant
already did, a summary of a long thread, similar past tickets and how they were
resolved, a draft reply — and, for anything that does not fit a prepared
command, a free-text conversation with a read-only agent.

Status: **prototype, unmerged.** A first slice lives on the branch
`widget-debug-console` (commit `533a19e`): `src/widget/` with one endpoint
`POST /api/widget/query`, a flat `WidgetService._COMMANDS` dict holding a
single `/summary` command, and a console in the admin UI
(`ui/src/WidgetConsole.tsx` + its host page `ui/src/Widget.tsx`). No agent, no
memory, no iTop sidebar yet. The branch predates the enrichment deletion and
needs a rebase onto `main` before anything is built on it — in particular its
`strip_thinking` move into `text_utils.py` has since landed on `main` anyway.

---

## 1. The load-bearing decision: two input paths

One console, two ways in, split by **who decided to act**:

| | Slash command (`/summary`, `/draft`, …) | Free text |
|---|---|---|
| Who chose the action | the engineer, explicitly | the model |
| Dispatch | deterministic — a registry lookup, no LLM decides *what* to do | a tool-calling agent decides |
| May write to iTop | **yes** | **no**, ever |
| Tools | the command's own fixed logic | read-only tools only |

This is the project's human-in-the-loop principle with the autonomy criterion
moved: elsewhere the assistant acts alone when the action is *reversible*; here
it acts when the action was *explicitly invoked*. An engineer who types
`/note ...` has authored that write. Text typed at an agent has not authorized
anything, so the agent physically cannot write — not because the prompt asks it
not to, but because no writing tool is in the set it is given. Same enforcement
model as intake: take the capability away rather than request good behaviour.

The console already has the seam. Today free text is answered with a hint that
only slash commands exist (`widget.hint_slash_command`); the agent path
replaces that branch.

---

## 2. Backend module shape

`src/widget/` is a peer of `src/admin/` — a synchronous query API, not a
webhook pipeline. It registers a `ModuleInfo` with an **empty route map**,
which is enough to get `/api/config/widget`, `/api/prompts/widget` and the
generic Modules/Prompts admin screens for free (already so on the branch,
`widget/module.py`).

```
assistant/src/widget/
├── module.py      # ModuleInfo registration (config + prompts discovery)
├── router.py      # POST /api/widget/query
├── commands.py    # CommandRegistry + the command handlers          (new)
├── agent.py       # build_widget_agent(): create_agent + checkpointer (new)
├── tools.py       # read-only tools for the agent path              (new)
├── context.py     # WidgetContext — per-request deps for tools      (new)
├── service.py     # ticket fetch, caching — shared by both paths
└── prompts.py     # WidgetPrompts + PROMPT_VARIABLES registry
assistant/prompts/widget/*.md
```

### 2.1 Slash commands: a registry, not a dict

The prototype's `_COMMANDS: dict[str, str]` (name → method name) is fine for
one command and wrong for five: it carries no metadata, so the router cannot
tell a reading command from a writing one, and the UI cannot list what exists.
Model it on `PipelineRegistry` / `ModuleInfo` (`src/pipelines/registry.py`) —
the pattern is already established and understood in this codebase:

```python
@dataclass(frozen=True)
class CommandInfo:
    name: str                 # "summary", without the slash
    description: str          # shown in the console's own /help
    handler: CommandHandler   # async (ctx, text) -> CommandResult
    read_only: bool           # False = this command writes to iTop
```

One `register()` call per command, and `GET /api/widget/commands` returns the
registry so the console renders `/help` and autocompletion from the backend
instead of duplicating the list in TypeScript — the same trick the LLM provider
form already uses with `GET /api/setup/llm-providers`.

`read_only` is not decoration: a writing command is the one place in this module
allowed to call a mutating `TicketRepository` method, and having the flag on the
record makes "did anything write?" answerable by the router and the audit log
rather than by reading each handler.

**Deliberately not designed: dynamic command loading.** Third-party commands as
independent installable packages, discovered without touching core code, is a
real idea and explicitly deferred — the registry gives code-level extensibility
(one line per command), which is what is actually needed. Revisit only when
someone genuinely needs to ship a command without a core change.

### 2.2 The agent path

```python
from langchain.agents import create_agent

agent = create_agent(
    model=create_llm(llm_cfg, cfg.agent_model),
    tools=READ_ONLY_TOOLS,
    context_schema=WidgetContext,
    checkpointer=deps.checkpointer,
    middleware=[ModelCallLimitMiddleware(run_limit=cfg.max_iterations,
                                         exit_behavior="end")],
)
```

Three points where this agent is deliberately **unlike** intake:

- **`create_agent`, not `langgraph.prebuilt.create_react_agent`.** The latter is
  deprecated in the installed langgraph ("moved to
  `langchain.agents.create_agent`") and the project standardized on
  `create_agent` when intake won its A/B. `create_agent` also takes
  `checkpointer=` directly, so §2.3 needs no drop into raw langgraph.
- **Prose is the product; do not force a tool call.** Intake forces
  `tool_choice="any"` wherever the endpoint accepts it because its plain text
  reaches nobody. Here plain text *is* the answer to the engineer, so
  `build_widget_agent` never passes `force_tool_choice` — the default `False`
  is correct and this is exactly the case CLAUDE.md anticipates when it says
  whether to force is the agent's call, not the connection's.
- **No terminal-tool middleware, no epilogue.** Intake ends its run the moment
  a terminal tool succeeds and closes the ticket if the model wandered off.
  Nothing here is terminal and nothing needs closing: the run ends when the
  model answers, and `ModelCallLimitMiddleware` is the only backstop needed.

Tool failures still deserve intake's treatment — a rejection that says what to
do instead, surfaced as an error `ToolMessage` rather than crashing the request.
Reuse the shape of `_tool_gate` (`agents/intake/agent.py`); a `ToolRejection`
equivalent belongs in `widget/tools.py`.

### 2.3 Memory for the agent path

Multi-turn memory comes from LangGraph's own checkpointer, keyed by
`thread_id = ticket.label` (`"UserRequest::123"`) — the same identity already
used for `lock:{ref}`, `TicketState` and journal entries, so a conversation is
per-engineer-session-per-ticket without inventing a new key space:

```python
config = {"configurable": {"thread_id": ticket.label}}
```

Rejected alternatives, for the record: **client-side-only history** (lost on
page reload, which disqualifies it as a working tool) and a **hand-rolled
conversation store** (LangGraph already solves this, and the project's own
`RunJournal` is a trace log, not a resumable state store).

Backend — a **new dependency either way**, to be added deliberately:

- `langgraph-checkpoint-redis` → `AsyncRedisSaver`
  (`langgraph.checkpoint.redis.aio`). Preferred: it has built-in TTL,
  `ttl={"default_ttl": <minutes>, "refresh_on_read": True}`, which lands almost
  exactly on the project's existing "Redis = TTL-bounded operational state"
  model (`state_ttl_days`, `run_ttl_days`). It accepts an existing
  `redis_client=`, so no second connection pool.
- `langgraph-checkpoint-postgres` — the alternative, and Postgres is already in
  the stack for pgvector. Heavier, but durable and queryable; pick it if
  conversations turn out to be worth keeping.

Two things to verify at implementation time, both cheap and both able to waste
an afternoon if missed:

1. `default_ttl` is in **minutes**, unlike every other TTL in this project
   (days) — convert at the boundary and name the config field accordingly.
2. The project's client is built with `decode_responses=True`
   (`deps.py:122`). Confirm `AsyncRedisSaver` tolerates that or give it its own
   client. `AsyncRedisSaver.asetup()` must also run once before use — the
   lifespan is the place.

`AppDeps` currently does not expose the raw Redis client (it is created inside
`build_deps` and handed to the stores). Adding the checkpointer means one new
`AppDeps` field, created in `build_deps` and closed in `aclose()` — the same
treatment `vector_db` already gets.

---

## 3. Read-only tools for the agent

The agent's power is entirely in this list, so it is worth being explicit about
each entry and its cost:

| Tool | Reads | Notes |
|---|---|---|
| `get_ticket_context` | `TicketState` + recent `RunJournal` entries for this ticket | what the assistant already did: classification, questions asked, handoff note. Zero LLM cost |
| `find_similar_tickets` | resolved/closed tickets, via the pgvector index | needs [vector-store](vector-store.md) Stage 4 — there is no non-vector mode |
| `get_service_context` | `Service` / `ServiceSubcategory` | the subcategory description is the desk's own definition of a complete ticket |
| `search_kb` | iTop `FAQ` | later; same retriever, different corpus — needs a KB `VectorSource` (vector-store Stage 6) |

`get_ticket_context` needs runs filtered by ticket ref. `RunJournal` lists runs
and filters in Python (`journal.py:107`); if that proves too slow, a secondary
index (`runs:by_ref:{ref}`) is the fix.

Note what is **not** here: no `post_public_log`, no `set_fields`, no
`create_problem`. Writes live in slash commands (§1).

### Similar tickets — semantic search, no keyword mode

An earlier draft of this plan had a keyword MVP (LLM-extracted search terms →
`title LIKE '%kw%'` OQL → rerank) with vectors as a later upgrade and keyword as
the permanent fallback. **Dropped.** The pgvector index is built and sweeping;
building an OQL keyword search now would mean writing, testing and then
maintaining forever a second retrieval path whose only job is to be worse than
the one that already exists. Multilingual embeddings are also the whole point
here — `LIKE '%принтер%'` does not find "printer", which is exactly the
service-desk case this feature exists for.

Retrieval is [vector-store.md §5](vector-store.md) end to end:

1. **Embed the query** — built from the current ticket's `profile` +
   `description` source text. One embeddings call.
2. **Filtered KNN**, one SQL statement: `GROUP BY obj_id` with `max(score)` so a
   ticket matching on both description and solution counts once and verbosity is
   not rewarded; layer-1 predicates for class, `similar_statuses`, visibility and
   org; `obj_id <> current`. Over-fetch (`similar_candidates`, default 15 for a
   `similar_top_n` of 5) because HNSW is approximate and pre-filters thin the
   `ef_search` frontier.
3. **Fresh fetch from iTop** for the surviving ids — the index holds no text, so
   title, status and solution come from the system of record at query time.
4. **Re-rank** (`similar_rerank` prompt): candidate titles + solution excerpts
   plus the current ticket → top-N with a one-line relevance reason.
   Non-parseable output degrades to raw KNN order.

`retriever.py` (steps 1–3) is vector-store Stage 4 and is a **hard prerequisite**
— this feature cannot ship before it, and there is no reduced mode that ships
earlier. `TicketRepository` gains the semantic field `solution` (+ a
`TicketFieldMap` entry, default `"solution"`) and a batch read by ids —
`fetch_many(obj_class, ids)`, the same `WHERE id IN (…)` shape
`find_existing_ids` already uses, but projecting the full mapped field set. The
`search(oql, …)` method the keyword design needed is not required. Tools and
services still never see iTop attribute names.

**When the index is unavailable, say so.** No `database_url`, no embeddings
connection, `vector.enabled` off, an empty index, or Postgres down — every one
of these makes `/similar` and `find_similar_tickets` return a plain "semantic
search is not configured" (or "the index is still building"), not silence and
not an empty result that reads like "no similar tickets exist". The command
registry should also hide `/similar` from `/help` while the feature cannot work,
so `/help` never advertises a dead command. This visible unavailability is what
replaces the keyword fallback, and it is the honest trade of dropping it:
similar-tickets becomes the one widget feature with an infrastructure
requirement.

---

## 4. Delivery vehicle: from admin console to iTop sidebar

The console lives in the admin UI **on purpose** — it lets the prompts, the
tools and the agent loop be iterated without touching iTop at all. The end
goal is unchanged: the same console inside the iTop backoffice, on the ticket
page the engineer is already looking at.

`WidgetConsole.tsx` is written for that move: it takes ticket context as props
and talks to `POST /api/widget/query`. Hosting it in iTop changes only where
the context comes from (page DOM instead of two text inputs); the component and
the endpoint contract stay.

```
┌─ Engineer's browser ────────────────────────────────┐
│  iTop backoffice page (UserRequest / Incident)      │
│  ┌───────────────────────────┐  ┌────────────────┐  │
│  │ iTop page DOM             │  │ AI console     │  │
│  │ .object-details           │◄─┤ (widget.js)    │  │
│  │   data-object-class       │  │ reads class/id │  │
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
│  /api/widget/*                                      │
│   • commands (may write) / agent (read-only)        │
│   • TicketRepository / CatalogRepository (iTop API)  │
│   • Redis: checkpointer + response cache            │
└─────────────────────────────────────────────────────┘
```

### Why a PHP proxy rather than browser→assistant calls

- **No secret in the browser.** `widget_token` lives in the iTop config file
  and is attached server-side.
- **Engineer authn for free.** The proxy enforces the iTop backoffice session
  (`LoginWebPage::DoLogin()`); the assistant trusts the proxy and receives the
  engineer's login in a header for audit.
- **No CORS, no mixed content.** The assistant never needs to be reachable
  from engineers' browsers — only iTop server → assistant, the same topology as
  webhooks, reversed.

Trade-off: no easy SSE streaming through the proxy. Responses are synchronous
JSON with a spinner; streaming is a later phase (chunked proxy or an optional
direct-CORS mode).

### iTop-side pieces

iTop 3.x extension interfaces (module `knowitop-ai-widget`):

- `iBackofficeLinkedScriptsExtension::GetLinkedScriptsAbsUrls()` → `widget.js`
- `iBackofficeLinkedStylesheetsExtension::GetLinkedStylesheetsAbsUrls()` → `widget.css`

`widget.js` self-activates only on object details pages: iTop 3.x renders
`div.object-details[data-object-class][data-object-id]` — if the class is in the
enabled list (fetched from the proxy's `config` route), render the panel;
otherwise do nothing. No server-side page hooks needed.

```
itop-extension/knowitop-ai-widget/
├── module.knowitop-ai-widget.php    # module declaration (datamodel-less)
├── main.knowitop-ai-widget.php      # the two *LinkedScripts/Stylesheets classes
├── ajax.knowitop-ai-widget.php      # session-guarded proxy to the assistant
└── asset/
    ├── widget.js                    # panel host + context extraction
    └── widget.css
```

```php
// config-itop.php module_settings
'knowitop-ai-widget' => array(
    'assistant_url' => 'http://assistant:8000',   // server-to-server URL
    'widget_token'  => '<same value as assistant security.widget_token>',
),
```

Proxy contract (`ajax.knowitop-ai-widget.php`):

- `LoginWebPage::DoLogin()` — backoffice session required.
- `?route=` whitelist: `config`, `commands`, `query`; anything else → 404.
  `class`/`id` pass through as-is — the assistant re-fetches the ticket from
  iTop anyway (system of record), so a tampered id leaks nothing the engineer
  could not open in iTop directly. (Optional hardening:
  `UserRights::IsObjectAllowedRead()` per id.)
- Adds `X-Auth-Token: widget_token` and
  `X-Itop-User: UserRights::GetUserLogin()`.
- Forwards the JSON body; returns the assistant's status/body verbatim; maps
  network errors to 502 with a short JSON error.

Deployment note: in the dev compose, mount `itop-extension/` into the iTop
container's `extensions/` dir and re-run the iTop setup once — iTop only picks
up new modules through setup.

---

## 5. Access control

Auth on `/api/widget/*` is `X-Auth-Token` == `security.widget_token` (a new
secret field; `None` = auth disabled plus a startup warning, same policy as
`webhook_token`). While the console is admin-UI-only, the existing admin token
covers it and `widget_token` can wait for the iTop extension.

`X-Itop-User` is logged with every request — which engineer asked for what.
The prototype already logs a client-supplied `user` field; that is acceptable
for a debug console behind the admin token and **must** become the
proxy-supplied header before the iTop extension ships, since a
client-supplied login is not an audit trail.

**Retrieval, once vector mode exists, does not get to define its own rights
model.** The three-layer scheme in [vector-store.md §4](vector-store.md) holds:
an org/visibility pre-filter in SQL is an optimization, and the authoritative
check is an iTop-side `check-read` oracle route executed in the engineer's own
session before any candidate content reaches the engineer *or an LLM prompt*.
The agent path makes that stricter, not looser: content the agent pulls into
its context has to clear the same check, because the model will paraphrase it
into the reply.

---

## 6. UX

The console is one text field, not a row of per-feature buttons. Two rules keep
it from reading as an opaque chat:

- **Visible tool trail.** An agent reply carries small chips above it naming
  the tools it used ("🔍 similar tickets → 📄 service context"), so the
  engineer can tell "the model decided this from these sources" from the output
  of a deterministic command. The response needs a `tools_used` field for this;
  `describe_ai_message` (`agents/intake/agent.py`) is the existing precedent for
  turning a model turn into a readable line.
- **`/help` from the registry.** The console lists commands (and their
  descriptions) from `GET /api/widget/commands`, so a newly registered command
  is discoverable without a frontend change.

Everything LLM-backed is **on demand** — a submitted line. Nothing is computed
because a page opened; `config` and `commands` are free reads. The input is
disabled while a request is in flight.

Draft replies are inserted into the engineer's own case-log textarea (or copied
to the clipboard) — the engineer reviews, edits and posts under their own name.
Widget output is advice, not action.

---

## 7. Caching and cost control

- Redis cache per command: `widget:cache:{command}:{class}:{id}` →
  `{content_hash, payload, ts}`, where `content_hash` covers title, description
  and public-log length, so a new comment invalidates naturally. TTL
  `widget.cache_ttl_minutes` (default 60).
- The agent path is **not** cached — it is a conversation, and the checkpointer
  is its state. `max_iterations` is the cost ceiling per turn.
- A per-ticket in-flight lock against duplicate concurrent LLM calls is a later
  hardening step; the disabled input covers the common case.

---

## 8. Config

```python
class WidgetConfig(BaseModel):
    enabled: bool = True
    classes: list[str] = ["UserRequest", "Incident"]
    # Free-text agent path
    agent_enabled: bool = True
    agent_model: str | None = None      # None = global llm.model
    max_iterations: int = 6
    memory_ttl_minutes: int = 60 * 24   # checkpointer TTL (minutes — see §2.3)
    # Slash commands
    summary_model: str | None = None
    # Similar tickets — semantic search only; the feature reports itself
    # unavailable when the vector index is not there (§3)
    similar_statuses: list[str] = ["resolved", "closed"]
    similar_candidates: int = 15         # KNN over-fetch before the rerank
    similar_top_n: int = 5
    cache_ttl_minutes: int = 60
```

No `similar_backend`: there is one retrieval path. Whether similar-tickets works
is a question about the infrastructure (`database_url`, the `embeddings` section,
`vector.enabled`, a non-empty index), and those already have their own settings —
a `widget.*` flag on top of them would only add a second place to get it wrong.

Plus `SecurityConfig.widget_token: str | None` (added to `SECRET_FIELDS`;
editable through the existing `/api/setup/security` endpoint and the
Connections UI with zero extra backend work).

`agent_enabled` earns its place: it is the switch that turns the console back
into a pure command dispatcher if the agent misbehaves in a deployment, without
a redeploy. All of it is runtime-editable through `/api/config/widget`, prompts
through `/api/prompts/widget` — both free from the `ModuleInfo` registration.

---

## 9. Implementation stages

### Stage 0 — land the prototype
- [ ] Rebase `widget-debug-console` onto `main` (the branch predates the
      enrichment deletion; its `strip_thinking` move is already on `main`).
- [ ] Rename `WidgetService._COMMANDS` into the `CommandRegistry` of §2.1,
      `/summary` as its first entry; `GET /api/widget/commands`.
- [ ] Console renders `/help` from that endpoint.
- [ ] Tests: registry (unknown command → 400, listing), `/summary` unchanged.

Exit criterion: same behaviour as the prototype, extensible shape.

### Stage 1 — the agent path
- [ ] `widget/context.py`, `widget/tools.py` with `get_ticket_context` and
      `get_service_context` (read-only), a `ToolRejection` equivalent and a
      `_tool_gate`-shaped middleware.
- [ ] `widget/agent.py`: `create_agent` + `ModelCallLimitMiddleware`, no forced
      `tool_choice`; router dispatches non-slash input here when
      `agent_enabled`.
- [ ] Response carries `tools_used`; console renders the trail chips.
- [ ] Prompts: `agent_system.md` with the placeholder registry and startup
      validation via `ModuleInfo.validate_prompts`.
- [ ] Tests: a scripted `FakeToolCallingModel` as in `test_intake_agent.py`
      (langchain-core's ready-made fakes leave `bind_tools` unimplemented);
      assert no writing tool is reachable and that `tool_choice` is *not*
      forced.

Note the consequence of dropping the keyword path: at this stage the agent has
only ticket and service context to work with — enough to answer "catch me up"
and "what has the assistant already done", not enough for "has anyone seen this
before". It becomes genuinely useful at Stage 3. Ship it anyway; the loop, the
tool trail and the memory are what need real-engineer feedback, and they can get
it on the two cheap tools.

### Stage 2 — memory
- [ ] Pick the checkpointer backend (§2.3), add the dependency, expose it on
      `AppDeps` (created in `build_deps`, `asetup()` in the lifespan, closed in
      `aclose()`).
- [ ] `thread_id = ticket.label`; `memory_ttl_minutes` wired.
- [ ] A console "reset conversation" action that drops the thread.
- [ ] Tests: two turns share context; a third after a reset does not.

### Stage 3 — similar tickets (blocked on vector-store Stage 4)
Prerequisite, not part of this stage: `vector/retriever.py` — embed → filtered
KNN → fresh fetch. Until it exists there is nothing to build here, and there is
deliberately no reduced version that ships earlier (§3).
- [ ] `TicketFieldMap.solution` + `TicketRepository.fetch_many(obj_class, ids)`.
- [ ] `find_similar_tickets` tool and `/similar` command over the retriever;
      `similar_candidates` over-fetch → `similar_rerank` prompt →
      `similar_top_n` with reasons; degrade to raw KNN order on parse failure.
- [ ] Unavailability is a message, not silence: no Postgres / no embeddings /
      `vector.enabled` off / empty index each produce a stated reason, and
      `/similar` disappears from `/help` while it cannot work.
- [ ] Tests: score aggregation per object, filter composition, rerank parse
      fallback, and each unavailability branch returning its reason rather than
      an empty list.

### Stage 4 — writing commands
- [ ] `/draft` (draft reply, returned for the engineer to post) and `/note`
      (writes a private-log note) — the first `read_only=False` command, so
      this is where the flag starts doing work.
- [ ] Audit: writing commands recorded with the engineer login. Consider
      journalling widget queries as `module="widget"` runs — `ProcessingRun`
      already has the field, so they would appear in the existing Runs screen
      for free.
- [ ] Tests: endpoint + prompt placeholders; a read-only command cannot reach
      a mutating repository method.

### Stage 5 — into iTop
- [ ] `itop-extension/knowitop-ai-widget/`: module + main + ajax proxy;
      `widget.js` hosting the console; `security.widget_token`;
      `X-Itop-User` replacing the client-supplied login.
- [ ] Compose: mount `itop-extension/`, document the one-time setup re-run.
- [ ] Verify: panel appears on UserRequest/Incident details only; the proxy
      reaches the assistant; an unauthenticated browser hit on the ajax page is
      rejected.
- [ ] `docs/widget.md`: extension install, config snippet, token wiring;
      README feature mention.

### Stage 6 — hardening and beyond
- [ ] Rights oracle (`check-read`) and the two-leg flow
      ([vector-store.md §4](vector-store.md)) — required before the console
      leaves the admin UI, since retrieved content reaches both the engineer and
      the model's context.
- [ ] `search_kb` over iTop `FAQ`; "this ticket looks KB-worthy" + draft.
- [ ] Feedback chips (👍/👎 per reply → journal) as prompt-tuning data.
- [ ] Streaming (chunked proxy or direct mode) if wait time becomes a complaint.

---

## 10. Open questions / assumptions

- **iTop version**: assumes 3.x backoffice (`data-object-class` DOM markers,
  `iBackofficeLinkedScriptsExtension` — 3.0+). 2.7 is out of scope.
- **Backoffice only**: no end-user portal console in this plan. A portal
  version would need §5 re-run with the portal rights model in scope.
- **Widget token distribution is manual** (iTop config file).
  `POST /provision-itop` cannot write iTop config files — accepted, documented.
- **`thread_id = ticket.label` means one conversation per ticket**, shared by
  every engineer who opens it. That is probably right for a service desk (the
  next engineer sees the reasoning), but it is a decision, not a detail — if it
  turns out wrong, the key becomes `f"{ticket.label}:{engineer_login}"`.
- **`solution` on Incident**: stock iTop has it on both UserRequest and
  Incident; customized datamodels handle it via `ticket_mapping`
  `class_overrides` as usual.
- **Prompt-editing story for the agent**: the system prompt is a prompt file,
  but tool docstrings are code — the same split as intake, and worth stating in
  `docs/prompts.md` when this ships.

## References

- iTop 3.0 extension interfaces overview:
  https://www.itophub.io/wiki/page?id=3_0_0%3Acustomization%3Aapi%3Aextensions%3Astart
- `iBackofficeLinkedScriptsExtension` docs (linked from the overview page).
