# Build context is the repository root (ui/ and assistant/ are siblings):
#   docker build -f assistant/Dockerfile .
#
# The build stages mirror that layout under /src rather than flattening it.
# Both the frontend's output path and the package's own metadata are written
# relative to the repository root, so a stage that holds only one directory
# resolves `..` to something that isn't there.

FROM node:22-alpine AS ui-builder

WORKDIR /src/ui

# Install dependencies first — this layer is cached unless the lockfile changes
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
RUN npm run build

# ---- python dependencies + the project itself ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /src/assistant

# The venv is built where it will finally live, not beside the project: a venv
# carries absolute paths in its script shebangs, so moving it between the build
# and runtime stages leaves them pointing at a directory that is not there and
# the entrypoint dies with "no such file or directory".
ENV UV_PROJECT_ENVIRONMENT=/app/.venv

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

# The package's description is the repository README, assembled at build time
# (hatch-fancy-pypi-readme in pyproject.toml), so the build needs the file that
# sits beside this directory in a checkout. Copied before the source because it
# changes far less often.
COPY README.md /src/README.md

# Install the application itself. --no-editable puts the package inside the
# venv, so the runtime image needs nothing but the venv.
COPY assistant/ ./
# The SPA is part of the package (ADR-032) — `npm run build` writes it here in
# a checkout, and here is where the wheel picks it up. Copied after the source
# so a frontend change does not invalidate the layers above, and so the build
# fails (hatch_build.py) if the stage above produced nothing.
COPY --from=ui-builder /src/assistant/src/itop_ai_assistant/ui_dist ./src/itop_ai_assistant/ui_dist
RUN uv sync --frozen --no-dev --extra tracing --no-editable

# ---- runtime image ----
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="iTop AI Assistant"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

# The venv already contains the application, its build stamp, default prompts,
# config.yaml and the admin SPA — no source, and no .git, reaches the runtime
# image
COPY --from=builder /app/.venv ./.venv

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "itop_ai_assistant.main:app", "--host", "0.0.0.0", "--port", "8000"]
