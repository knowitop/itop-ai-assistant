"""Async Postgres access for the vector store.

The vector store is optional: `database_url` is a bootstrap (env-only)
setting, and when it is unset the assistant runs Redis-only. `VectorDb` is
therefore always present in `AppDeps` but creates the engine lazily, so the
app boots with no Postgres at all and consumers degrade via `configured`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class VectorDbNotConfigured(RuntimeError):
    """Raised when Postgres is accessed while `database_url` is unset."""


class VectorDb:
    """Lazy holder of the async Postgres engine (unconfigured when DSN is None)."""

    def __init__(self, database_url: str | None) -> None:
        self._url = database_url
        self._engine: AsyncEngine | None = None

    @property
    def configured(self) -> bool:
        return self._url is not None

    @property
    def engine(self) -> AsyncEngine:
        if self._url is None:
            raise VectorDbNotConfigured("database_url is not set — the vector store is unavailable")
        if self._engine is None:
            self._engine = create_async_engine(self._url, pool_pre_ping=True)
        return self._engine

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.connect() as conn:
            yield conn

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None


def run_migrations(database_url: str) -> None:
    """Apply Alembic migrations up to head.

    Synchronous (alembic drives its own event loop) — call via
    `asyncio.to_thread` from async code. The URL travels through
    `config.attributes` to avoid configparser interpolation of `%` in
    passwords.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")
    logger.info("Vector store migrations applied (alembic upgrade head)")
