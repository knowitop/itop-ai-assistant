# Build context is the repository root (ui/ and assistant/ are siblings):
#   docker build -f assistant/Dockerfile .

FROM node:22-alpine AS ui-builder

WORKDIR /ui

# Install dependencies first — this layer is cached unless the lockfile changes
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
RUN npm run build

# ---- python dependencies ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# Install dependencies into a separate venv — this layer is cached unless lockfile changes
COPY assistant/pyproject.toml assistant/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- runtime image ----
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="iTop AI Assistant"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code, default config and the admin UI build
COPY assistant/src ./src
COPY assistant/prompts ./prompts
COPY assistant/config.yaml ./
COPY --from=ui-builder /ui/dist ./ui/dist

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
