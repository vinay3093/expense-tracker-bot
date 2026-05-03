"""Build a SQLAlchemy ``Engine`` from settings.

One factory, one place to tune connection pool defaults so the
Postgres edition behaves predictably whether running:

* Locally (one process, low concurrency).
* On Supabase (transactional pgbouncer pooler, watch out for
  prepared-statement caching).
* In CI tests (SQLite-in-memory, single connection per test).
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool, StaticPool

from ...config import Settings, get_settings
from .exceptions import PostgresConfigError


def get_engine(settings: Settings | None = None) -> Engine:
    """Return a configured :class:`sqlalchemy.Engine`.

    The connection URL comes from ``settings.DATABASE_URL``.  Pool
    settings are picked based on the URL scheme:

    * ``sqlite://`` (in-memory) — :class:`StaticPool` so all
      sessions in the test process share the same connection (each
      ``:memory:`` connection is a fresh DB).
    * ``sqlite:///path`` (file-backed) — default file pool.
    * ``postgresql+psycopg://...`` against Supabase's pgbouncer
      pooler — :class:`NullPool` so we don't pin connections (the
      pooler does its own pooling; double-pooling causes "prepared
      statement already exists" errors).
    * ``postgresql+psycopg://...`` direct (no bouncer) — the
      default :class:`QueuePool`, sized small (5/10) for a personal
      bot.

    Raises:
        PostgresConfigError: ``DATABASE_URL`` is missing or empty.
    """
    cfg = settings or get_settings()
    if cfg.DATABASE_URL is None:
        raise PostgresConfigError(
            "DATABASE_URL is not set.  For the Postgres edition you need "
            "something like: "
            "postgresql+psycopg://USER:PASS@HOST:PORT/DB_NAME .  "
            "On Supabase, copy the 'Transaction pooler' connection string "
            "from Project Settings -> Database."
        )
    url = cfg.DATABASE_URL.get_secret_value().strip()
    if not url:
        raise PostgresConfigError("DATABASE_URL is empty.")

    if url.startswith("sqlite:///:memory:") or url == "sqlite://":
        # All sessions must share one connection in :memory: mode.
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
    if url.startswith("sqlite:"):
        return create_engine(url)
    # Supabase / Neon transactional poolers: NullPool keeps us from
    # double-pooling.  Heuristic: hostname contains "pooler" or the
    # port is 6543 (Supabase pooler default).
    use_null_pool = ("pooler" in url) or (":6543/" in url)
    if use_null_pool:
        return create_engine(url, poolclass=NullPool, future=True)
    return create_engine(
        url, pool_size=5, max_overflow=10, pool_pre_ping=True, future=True,
    )


__all__ = ["get_engine"]
