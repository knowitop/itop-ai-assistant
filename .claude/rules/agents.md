---
paths:
  - "assistant/src/itop_ai_assistant/agents/**"
  - "assistant/src/itop_ai_assistant/graph/**"
  - "assistant/src/itop_ai_assistant/prompts/**"
  - "assistant/src/itop_ai_assistant/settings/prompt_store.py"
  - "assistant/src/itop_ai_assistant/core/llm_providers.py"
---

# Agents, tools and prompts

Implementation of the intake module: `dev-docs/architecture/intake.md`.
LLM stack and prompt loading: `dev-docs/architecture/config-and-setup.md`.

## Where a module goes

`agents/` — the model decides the order. `graph/` — the code does. Keep the
convention; `graph/` is reserved for a genuinely deterministic multi-step flow.

## Tools

- One invariant per writing tool, enforced **inside** the tool. Reject a bad call
  with a `ToolRejection` that says what to do instead.
- Counters (rounds, budgets) are set by code, never by the model.
- The tool set is **per run**: withhold a tool instead of asking the prompt to
  avoid it (`tools_for(ticket)`). Taking the tool away is what actually works.
- **Tool docstrings are code, not prompts.** They must stay in sync with the
  signature and with the invariant enforced inside.
- Tools take everything from `runtime.context` — never globals, never
  `get_settings()`.

## Traps

- Returning `Command(goto="__end__")` from `wrap_tool_call` does **not** end the
  run: the conditional edge `create_agent` puts on the tools node fires anyway.
- A model that must call a tool still answers in prose sometimes. Keep
  `_require_tool_call` on even when `tool_choice` is forced — Ollama and some
  gateways drop the field silently instead of erroring.
- Whether an endpoint accepts a forced `tool_choice` is a fact about the
  connection (`core/llm_providers.py`); whether to use it is the agent's call.
  An agent whose prose is the product passes `force_tool_choice=False`.

## Prompts

Files, not code: package defaults in `agents/<module>/prompts/`, deployment
overrides by same-named file under `<prompts_dir>/<module>/`, re-read every
run.

A new placeholder requires an entry in the module's `PROMPT_VARIABLES` and a
value passed where the messages are built — otherwise startup validation fails
the boot. After touching `agents/intake/prompts/*.md` or a tool signature, run
`uv run pytest test/integration` (needs a real endpoint).

## LLM client

`create_llm` (`core/deps.py`) is the only construction site and returns
`BaseChatModel`; type every consumer that way. Adding a provider is an entry in
`core/llm_providers.py` plus its `langchain-*` package — never a change in an
agent.
