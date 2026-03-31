FROM ghcr.io/astral-sh/uv:python3.13-bookworm
LABEL authors="vladimir.kunin"

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV PYTHONPATH=/app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src


EXPOSE 8000

CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
