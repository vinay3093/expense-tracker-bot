"""Alembic environment for the Postgres + NocoDB edition.

Hand-rolled (not the boilerplate ``alembic init`` template) so it
works against any :class:`Engine` you give it — the production
Postgres URL, a local docker Postgres for testing, *or* SQLite for
the test suite.

Two run modes (Alembic standard):

* **Online**  (``alembic upgrade head`` against a real DB) — opens a
  connection from :func:`get_engine` and runs migrations inside it.
* **Offline** (``alembic upgrade head --sql``) — emits raw SQL
  to stdout so DBAs can review before running.

The migrations themselves live in :mod:`...migrations.versions`.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from expense_tracker.ledger.nocodb.factory import get_engine
from expense_tracker.ledger.nocodb.models import Base

config = context.config

# Optional logging — only configure if the .ini specifies it.  Pass
# disable_existing_loggers=False so we don't wipe loggers created by
# the parent process (chat pipeline, pytest's caplog, ...).  Without
# this, running ``alembic upgrade`` inside the test suite silently
# breaks every later test that asserts on log records.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without opening a real DB connection.

    Used by ``alembic upgrade head --sql`` to produce a migration
    script for review.  Falls back to the active ``DATABASE_URL`` if
    the .ini doesn't override ``sqlalchemy.url``.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = str(get_engine().url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Open a real connection and apply migrations against it.

    Connection precedence:

    1. ``sqlalchemy.url`` set on the Alembic config (e.g. by tests
       passing a fresh tmp_path SQLite file) — wins.
    2. ``DATABASE_URL`` env var via :func:`get_engine` — production.
    """
    url_override = config.get_main_option("sqlalchemy.url")
    if url_override:
        from sqlalchemy import create_engine
        connectable = create_engine(url_override, poolclass=pool.NullPool)
    else:
        connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            poolclass=pool.NullPool,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
