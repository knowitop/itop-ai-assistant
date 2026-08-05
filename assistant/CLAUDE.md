# Development Commands

All commands run from `assistant/` unless noted.

```bash
uv sync                                # deps (--no-dev for production)
uv run uvicorn itop_ai_assistant.main:app --host 0.0.0.0 --port 8001 --reload
uv run pytest                          # unit tests (the default suite)
uv run pytest -k "test_name"           # one test
uv run ruff check . && uv run ruff format .
cd ../docker && docker-compose up -d   # full stack: iTop + assistant + Redis
```

**Before pushing**, run what CI runs: `uv run pre-commit run --all-files` and
`uv run pytest`. Note that `pre-commit run mypy --all-files` is the **strict**
type gate — `uv run mypy src/` is not the same check and passing it proves
less. CI (`.github/workflows/ci.yml`) runs on every push to `main` and every PR
and gates the image publish; it adds `pytest test/pg` and `npm run build` for the
UI. `test/integration` needs a real model endpoint and is excluded there.
