---
paths:
  - "assistant/test/**"
  - "assistant/pytest.toml"
---

# Testing

- Unit tests live in `test/unit/`, mirroring the module tree
  (`test/unit/agents/<module>/`) where a module owns tests, flat otherwise, and
  are the only suite collected by default (`testpaths`). `pytest.toml` sets
  `importmode = importlib`; the package is on the path because `uv sync`
  installs it editable.
- LLM calls and HTTP are mocked (`ItopMockTransport`), Redis is `fakeredis`.
- `get_settings()` is `lru_cache`d — call `get_settings.cache_clear()` in
  `setUp`/`tearDown` when a test controls env vars.
- `test/pg/` (Testcontainers, `pgvector/pgvector:pg17`) and `test/integration/`
  (a **real** LLM, needs `.env.test`) are not collected by default. Run them
  explicitly: `uv run pytest test/pg`, `uv run pytest test/integration`.
- The intake agent loop is driven by a scripted `FakeToolCallingModel`
  (`test_intake_agent.py`) — `create_agent` calls `bind_tools`, which
  `BaseChatModel` leaves unimplemented, so the ready-made langchain-core fakes do
  not fit. Those tests call `IntakeRun.body(...)` directly and set `run.repos`
  by hand; `execute()` is the shell's job and is covered separately.
- Tools are called as `tools.<name>.coroutine(...)`, bypassing pydantic.
- **Anything generic is pinned by two implementations.** The shell is tested
  through intake *and* a probe subclass; the request and schedule entry points
  through the real module *and* a probe route on a throwaway registry — the
  endpoint must know a registry entry, not a module by name. Keep that when
  adding a core seam.
- `test_scheduler.py` never sleeps for real time: intervals are microscopic and
  every wait is on an `asyncio.Event` the tick sets. Do not introduce real sleeps.
