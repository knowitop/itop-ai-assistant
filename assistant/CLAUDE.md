# Development Commands

All commands run from `assistant/` unless noted.

```bash
uv sync                                # deps (--no-dev for production)
uv run uvicorn itop_ai_assistant.main:app --host 0.0.0.0 --port 8001 --reload
uv run pytest                          # unit tests (the default suite)
uv run pytest -k "test_name"           # one test
uv run ruff check . && uv run ruff format .
cd ../docker && docker-compose up -d   # full stack: iTop + assistant + Redis + Qdrant
```

**Before pushing**, run what CI runs: `uv run pre-commit run --all-files` and
`uv run pytest`. Note that `pre-commit run mypy --all-files` is the **strict**
type gate — `uv run mypy src/` is not the same check and passing it proves
less. CI (`.github/workflows/ci.yml`) runs on every PR, and `release.yml` calls
it on every push to `main` and every tag — nothing reaches ghcr, Docker Hub or
PyPI unless it passes. It adds `npm run build` for the UI. `test/integration`
needs a real model endpoint and is excluded there.

**Live verification on the stand** (`docker-compose up`, manual checks against
the running stack) happens only after the user confirms — don't start the
stack or poke it unprompted, even to "just double-check" a change. Unit tests
and `pre-commit` are enough to report work as done; note in the summary that
live verification is still pending.
