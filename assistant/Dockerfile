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

# Install dependencies into a separate venv — this layer is cached unless lockfile changes
COPY assistant/pyproject.toml assistant/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# Install the application itself. --no-editable puts the package inside the
# venv, so the runtime image needs nothing but /app/.venv.
COPY assistant/ ./
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime image ----
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="iTop AI Assistant"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

# The venv already contains the application, its default prompts and config.yaml
COPY --from=builder /app/.venv /app/.venv

# The admin SPA is not a Python artifact — it is copied alongside
COPY --from=ui-builder /ui/dist ./ui/dist

ENV PATH="/app/.venv/bin:$PATH"
ENV UI_DIST_DIR=/app/ui/dist

# Build stamp. .git is excluded from the build context, so the version cannot
# be derived here — CI passes it from the tag it is building. Declared last so
# a changed stamp does not invalidate the layers above. A local build without
# these arguments honestly reports "dev".
ARG APP_VERSION=dev
ARG BUILD_COMMIT=""
ENV APP_VERSION=${APP_VERSION} BUILD_COMMIT=${BUILD_COMMIT}

EXPOSE 8000

CMD ["uvicorn", "itop_ai_assistant.main:app", "--host", "0.0.0.0", "--port", "8000"]
