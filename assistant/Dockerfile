# Build context is the repository root (ui/ and assistant/ are siblings):
#   docker build -f assistant/Dockerfile .

FROM node:22-alpine AS ui-builder

WORKDIR /ui

# Install dependencies first — this layer is cached unless the lockfile changes
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
RUN npm run build

# ---- python dependencies + the project itself ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# hatch-vcs shells out to git to derive the version from the tag
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Mirrors the repository layout: the build reads ../.git relative to pyproject.
# The venv is pinned to its runtime location — entry point shebangs are
# absolute, so it cannot be relocated after the fact.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src/assistant

# Install dependencies into a separate venv — this layer is cached unless lockfile changes
COPY assistant/pyproject.toml assistant/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# The whole repository, not just assistant/: the version and the commit are
# derived from git at build time (hatch_build.py), and a partial work tree
# reads as dirty, which would turn a tagged release into a .devN version.
COPY . /src/

# Install the application itself. --no-editable puts the package inside the
# venv, so the runtime image needs nothing but the venv.
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime image ----
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="iTop AI Assistant"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

# The venv already contains the application, its build stamp, default prompts
# and config.yaml — no source, and no .git, reaches the runtime image
COPY --from=builder /app/.venv /app/.venv

# The admin SPA is not a Python artifact — it is copied alongside
COPY --from=ui-builder /ui/dist ./ui/dist

ENV PATH="/app/.venv/bin:$PATH"
ENV UI_DIST_DIR=/app/ui/dist

EXPOSE 8000

CMD ["uvicorn", "itop_ai_assistant.main:app", "--host", "0.0.0.0", "--port", "8000"]
