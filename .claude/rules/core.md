---
paths:
  - "assistant/src/itop_ai_assistant/pipelines/**"
  - "assistant/src/itop_ai_assistant/schedule/**"
  - "assistant/src/itop_ai_assistant/webhook/**"
  - "assistant/src/itop_ai_assistant/request/**"
  - "assistant/src/itop_ai_assistant/main.py"
  - "assistant/src/itop_ai_assistant/deps.py"
  - "assistant/src/itop_ai_assistant/background.py"
  - "assistant/src/itop_ai_assistant/api_deps.py"
  - "assistant/src/itop_ai_assistant/principal.py"
  - "assistant/src/itop_ai_assistant/journal.py"
---

# The run core

How the seams are meant to work: `dev-docs/architecture/platform.md` §3.1–3.2.
Which file implements what: `dev-docs/reference/source-map.md`.

## No module-level singletons

`build_deps()` assembles every shared dependency at startup (FastAPI lifespan,
`app.state.deps`); a run builds its own context with a config snapshot and a
per-run LLM client. Nothing reads globals or `get_settings()` at call time.

## Connections: `for_principal()`, not `get()`

Inside a run, always `deps.itop.for_principal(principal, comment=...)`. A bare
`get()` compiles and works — and silently acts as the service account with no
change comment. `get()` is for code that is genuinely not a run: the vector
sweep, the wizard probes.

`ai_person_name()` answers off the **service** bundle whatever the run acts as.
Resolving it under an engineer's token would make the loop guard — which
compares it against the author of the last public comment — lie.

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
Both kinds of loop are assembled in `background.py`, one line each. Loops are
**per process** — cross-replica exclusion is the tick's own business.

`ModuleInfo` (what a business module is) and the routes (what may start a run)
live in the same file and are deliberately not the same thing.

## Adding a module

`agents/<module>/pipeline.py` with `register(registry, settings)` exposing a
`ModuleInfo` and its routes, one call in `build_registry`, one config section in
`config.py`. Object-scoped work subclasses `TicketRun`; work that is not about an
object is a plain coroutine `(run, deps) -> RunOutcome`. Every handler takes a
`RunContext`, never a bare id. `validate_prompts` runs at startup for every
module, so a broken template fails the boot instead of a live ticket.

`agents/selfcheck/` is the reference implementation of this contract.
