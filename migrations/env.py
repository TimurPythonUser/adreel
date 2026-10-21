"""Alembic: синхронные миграции по URL из настроек."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from adreel.config import settings
from adreel.db.models import Base
from adreel.db.session import sync_url

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", sync_url(settings.db_url))

target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = engine_from_config(config.get_section(config.config_ini_section, {}),
                                prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        # render_as_batch: SQLite не умеет ALTER COLUMN, Alembic пересоздаёт таблицу
        context.configure(connection=conn, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
