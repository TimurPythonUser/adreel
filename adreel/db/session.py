"""Подключение к БД и миграции."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ..config import ROOT

Sessions = async_sessionmaker


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        path = url.split(":///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            # Без foreign_keys SQLite молча игнорирует ON DELETE CASCADE.
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return engine


def make_sessions(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def sync_url(url: str) -> str:
    """Alembic работает синхронно: sqlite+aiosqlite -> sqlite."""
    return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


def upgrade(url: str) -> None:
    """Довести схему БД до последней миграции. Вызывается при старте бота."""
    if url.startswith("sqlite"):
        path = url.split(":///", 1)[-1]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", sync_url(url))
    command.upgrade(cfg, "head")
