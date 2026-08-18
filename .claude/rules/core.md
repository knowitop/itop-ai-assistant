---
paths:
  - "assistant/src/itop_ai_assistant/pipelines/**"
  - "assistant/src/itop_ai_assistant/schedule/**"
  - "assistant/src/itop_ai_assistant/webhook/**"
  - "assistant/src/itop_ai_assistant/request/**"
  - "assistant/src/itop_ai_assistant/main.py"
  - "assistant/src/itop_ai_assistant/core/**"
  - "assistant/src/itop_ai_assistant/state/journal.py"
  - "assistant/src/itop_ai_assistant/itop/connection.py"
---

# The run core

How the seams are meant to work: `dev-docs/architecture/platform.md` §3.1–3.2.
Which file implements what: `dev-docs/reference/source-map.md`.

## The core takes ports, not the container

`ADR-019`. `AppDeps` goes to entry points and no deeper. The core is constructed
with the narrow protocols of `pipelines/ports.py` — `TicketRun` with `LockPort`,
`AiIdentity`, `StepJournal`, plus the concrete `ItopRepositories`; `AgentRun`
with `StepJournal`; `journalled_run` with `RunFrameJournal`. Not every port is
a `Protocol`: `ItopRepositories` has one real implementation and a surface
already as narrow as a protocol would make it, so wrapping it in one would add
nothing (`ADR-022`) — `AiIdentity` stays a `Protocol` because it hides most of
`ItopConnection` (`client()`/`as_principal()`/`aclose()`), the honest reason
the rule exists at all. A module's `<Run>.handle` is where the container is
taken apart into them, because the registry has one handler shape for every
module.

- **A protocol member is a method or a read-only `@property`, never a plain
  attribute** — attributes are invariant, and `AppDeps` (whose fields are typed
  concretely) would stop satisfying the port. Strict mypy catches this.
- **Nothing implements a protocol port by inheritance at the composition
  root.** `AppDeps` satisfies them structurally; making it inherit would drag
  Redis/Qdrant/langchain back into the core. This is about `AppDeps`, not
  about every class anywhere: `ItopConnection(AiIdentity)` (`itop/connection.py`)
  inherits its own port explicitly, declared in the same file — it drags in
  nothing the file didn't already import (`ADR-022`).
- **`aclose()` belongs to no port.** The pool is the composition root's, and a
  run must not be typed into closing it (`test_pipelines_ports.py`).
- Need something `RunDeps` does not carry? Declare a port at the consumer, as
  `handle_assigned` (`_AssignedDeps`) does — do not widen `RunDeps`. A
  disjoint five-member slice like the vector sweep's dependencies does not
  earn a protocol at all: `VectorIndexer`/`register_vector_sweep` just take
  the five as explicit parameters (TASK-039) — rule 3.2's criterion is
  cohesion, not member count, and five unrelated dependencies fail it.

## No module-level singletons

`build_deps()` assembles every shared dependency at startup (FastAPI lifespan,
`app.state.deps`); a run builds its own context with a config snapshot and a
per-run LLM client. Nothing reads globals or `get_settings()` at call time.

## Connections: always `for_principal()`

Inside a run, always `deps.itop.for_principal(principal, comment=...)`, which
answers with a `RepositorySet` — one identity, one comment, every repository in
it bound to the same client. Code that is genuinely not a run — the vector
sweep, `selfcheck` — passes `Principal.service()` with a comment naming the
subsystem instead of a run id. The wizard probes bypass repositories entirely
and talk to `create_itop_client` directly, so they are not part of this seam.

`ai_person_name()` answers off the **connection's own** client whatever the run
acts as. Resolving it under an engineer's token would make the loop guard —
which compares it against the author of the last public comment — lie. That is
why `IdentityRepository` is built by `ItopConnection` and is not a member of
`RepositorySet`: from inside a run it is not merely wrong to call, it is
absent. A run reaches it as `RunDeps.ai_identity` (`AiIdentity`,
`itop/connection.py`), a separate property from `RunDeps.itop` — mixing
"repositories for an arbitrary principal" and "the name of the one fixed
service persona" into a single property was the wrong shape (`TASK-038`,
`ADR-022`).

## A class owns one config section

`ItopConnection` owns `itop`; `ItopRepositories` owns `ticket_mapping` /
`faq_mapping` and is the only place repositories are listed. Do not move a
section to whoever finds it convenient: while one fingerprint covered all three,
a mapping edit rebuilt the client and closed the pool (TASK-027). A new
repository is one line in `ItopRepositories._build`, not a second list.

## The shell is the core, not a module's business

- A module subclasses `TicketRun`, implements `stop_reason` and `body`, and
  registers `<Run>.handle`. The `lock` / `fetch` / `guard` journal steps are
  written by the shell, so every module leaves the same trace.
- **One instance per run, never per registration.** The reference, ids, deps and
  `bundle` live on the instance; a shared instance in the registry would race
  between concurrent triggers.
- `TicketRun` and `AgentRun` are **composed, not inherited** — a synchronous
  module needs the second half without the first.
- Every stop reports itself twice: a journal step and a `RunOutcome` with the
  same text, so a synchronous caller learns why nothing happened.
- Body exceptions **deliberately** propagate out of `execute()`. The entry point
  decides what a failure means: the webhook logs it (iTop already got its 202),
  the request lets it become a 500. Do not swallow them in the shell.

## Registry

`TriggerRegistry` is a startup-built map of what may start a run: `webhook`
`(class, event)`, `request` `(module, action)`, `schedule` `(module, name)`.
Every entry point rejects anything no module has claimed.

**A background loop is not a trigger.** `PeriodicTasks` paces periodic loops and
knows nothing about runs. Never put an infrastructure loop (the vector sweep)
into the registry: it would need a fake module name and a "do not journal" flag.
Both kinds of loop are assembled in `core/background.py`, one line each. Loops
are **per process** — cross-replica exclusion is the tick's own business.

`ModuleInfo` (what a business module is) and the routes (what may start a run)
live in the same file and are deliberately not the same thing.

## Adding a module

`agents/<module>/pipeline.py` with `register(registry, settings)` exposing a
`ModuleInfo` and its routes — `build_registry` finds it by that shape alone
(`pipelines/registry.py::discover_pipeline_modules`), nothing to add there by
name. Object-scoped work
subclasses `TicketRun`; work that is not about an object is a plain coroutine
`(run, deps) -> RunOutcome`. Every handler takes a `RunContext`, never a bare
id. `validate_prompts` runs at startup for every module, so a broken template
fails the boot instead of a live ticket.

A module's config model is its own: define it in `agents/<module>/config.py`,
never in `config.py`. `Settings` carries no field for it — `config.py` does
not know the model exists. Defaults come from `settings.module_defaults(name,
Model)` (validates `settings.module_config[name]`, empty dict if unset);
`RedisConfigStore` calls the same method as its fallback when resolving
`config_store.get(name, Model)`, so Redis overrides layer on top exactly like
any other section. A module reads its own `enabled`/other startup-gating
fields the same way, before the registry exists — `settings.module_defaults`
needs nothing but `Settings` itself, so there is no ordering problem between
building the registry and reading the config that decides what goes into it.

`agents/selfcheck/` is the reference implementation of this contract.
