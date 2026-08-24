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

WORKDIR /app

# Install dependencies into a separate venv — this layer is cached unless lockfile changes.
# `--extra tracing` even though tracing is off by default (TRACING_ENABLED): an
# image without the packages could not be told to trace at all, and a
# deployment cannot pip-install into a published image. Cheap here — the extra
# is the OTel SDK, the HTTP exporter and the OpenInference conventions, no gRPC.
COPY assistant/pyproject.toml assistant/uv.lock ./
RUN uv sync --frozen --no-dev --extra tracing --no-install-project --no-editable

# The build stamp, baked into the package by hatch_build.py and readable at
# run time through importlib.metadata and _build_info.py. No git here: the
# tag is something CI already knows, so it is handed over rather than
# rediscovered from history. Unset (a local build) yields the 0.0.0 fallback.
# BUILD_CHANNEL is what telemetry gates on: `release` only from the publish
# workflow, so an image built here by hand reports nothing.
ARG APP_VERSION=""
ARG BUILD_COMMIT=""
ARG BUILD_CHANNEL=""
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${APP_VERSION} BUILD_COMMIT=${BUILD_COMMIT} BUILD_CHANNEL=${BUILD_CHANNEL}

# Install the application itself. --no-editable puts the package inside the
# venv, so the runtime image needs nothing but /app/.venv.
COPY assistant/ ./
RUN uv sync --frozen --no-dev --extra tracing --no-editable

# ---- runtime image ----
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="iTop AI Assistant"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

# The venv already contains the application, its build stamp, default prompts
# and config.yaml — no source, and no .git, reaches the runtime image
COPY --from=builder /app/.venv ./.venv

# The admin SPA is not a Python artifact — it is copied alongside
COPY --from=ui-builder /ui/dist ./ui/dist

ENV PATH="/app/.venv/bin:$PATH"
ENV UI_DIST_DIR=/app/ui/dist

EXPOSE 8000

CMD ["uvicorn", "itop_ai_assistant.main:app", "--host", "0.0.0.0", "--port", "8000"]
