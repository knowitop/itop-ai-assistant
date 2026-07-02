FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# Install dependencies into a separate venv — this layer is cached unless lockfile changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- runtime image ----
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="iTop AI Assistant"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code and default config
COPY src ./src
COPY prompts ./prompts
COPY config.yaml ./

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]