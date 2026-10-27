# Contributing

`itop-ai-assistant` is a Python service (`assistant/`) with a React admin SPA
(`ui/`) and a compose stack (`docker/`). Start from `README.md` for what the
system does and from `docs/setup.md` for getting it running.

## Local development

Requires [uv](https://docs.astral.sh/uv/).

```bash
cd assistant
uv sync
cp ../docker/.env.dist .env   # fill in LLM and iTop settings
uv run uvicorn itop_ai_assistant.main:app --host 0.0.0.0 --port 8001 --reload
```

**Tests:**

```bash
uv run pytest                    # unit tests (mocked LLM, iTop and Redis), with coverage
uv run pytest test/integration   # the agent against a real LLM (needs .env.test)
```

Only the unit tests run by default; the integration suite is opt-in because it needs a reachable model endpoint.

**Admin UI** (requires Node.js; the dev server proxies `/api` to the backend on `:8001`):

```bash
cd ui
npm ci
npm run dev     # hot-reload dev server
npm run build   # production build, straight into the Python package
```

Architecture details and development conventions are in [CLAUDE.md](CLAUDE.md).

## Branch names

```
<type>/[task-NNN-]<slug>
```

Lowercase throughout, words joined by hyphens.

| Part | Meaning |
|---|---|
| `type` | one of `feat` `fix` `docs` `refactor` `test` `chore` `hotfix` |
| `task-NNN` | the `dev-docs/tasks/TASK-NNN-*` entry the branch closes — include it whenever the work has a task, omit it otherwise |
| `slug` | what the branch does, two to four words |

```
feat/task-062-telemetry-daily-counters
fix/task-060-placeholder-classification
refactor/task-023-package-layers
docs/config-switch-layer
chore/pre-commit-bump
```

Never commit to `main` directly — branch first.

Both rules are enforced by the `no-commit-to-branch` `pre-commit` hook: a commit
on `main`, or on a branch whose name does not match, fails. Rename with
`git branch -m <new-name>`. A detached `HEAD` (rebase, bisect, CI checkout) is
exempt.

## Hooks

`.pre-commit-config.yaml` sits at the repo root — its hooks cover the whole
repository, not just `assistant/`. `pre-commit` itself is a dev dependency of
the `assistant` project, and it falls back to the root config when the working
directory has none, so it runs from either place:

```bash
cd assistant && uv run pre-commit install    # once per clone
```

## Before you push

Run what CI runs, from `assistant/`:

```bash
uv run pre-commit run --all-files    # ruff, mypy (strict), import-linter, branch name
uv run pytest                        # unit tests
```

`uv run mypy src/` is *not* the same check as the pre-commit `mypy` hook, and
passing it proves less. `test/integration` needs a real model endpoint and is
excluded from CI. CI also builds the UI (`npm run build` in `ui/`).

## Pull requests

One branch, one task. Name the `TASK-NNN` the PR closes in its description; if
there is no task, say in a sentence what prompted the change.
