"""Smoke tests for the Alembic migration scaffold.

We don't try to verify every column type — that's the model's job and
``test_ledger_postgres.py`` already covers it.  These tests verify
the scaffold itself works:

* ``alembic upgrade head`` against a fresh SQLite file creates both
  expected tables.
* ``alembic downgrade base`` cleans them up.
* The migration's table shape matches what the SQLAlchemy models
  would have produced via ``create_all`` (catches model / migration
  drift early).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from expense_tracker.ledger.nocodb.models import AuditLog, Base, Transaction


@pytest.fixture()
def alembic_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """An Alembic Config pointing at a one-off SQLite file.

    We set ``sqlalchemy.url`` directly on the config rather than
    relying on ``DATABASE_URL`` + ``get_settings()``, because
    ``get_settings`` is ``lru_cache``\\d and would otherwise pin the
    first test's URL for every subsequent test.  ``env.py`` honours
    the explicit URL when present.
    """
    db = tmp_path / "alembic.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("STORAGE_BACKEND", "nocodb")

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option(
        "script_location",
        str(
            repo_root
            / "src"
            / "expense_tracker"
            / "ledger"
            / "nocodb"
            / "migrations",
        ),
    )
    return cfg


def test_upgrade_head_creates_both_tables(
    alembic_cfg: Config, tmp_path: Path,
) -> None:
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(os.environ["DATABASE_URL"])
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"transactions", "transactions_audit_log", "alembic_version"}.issubset(tables)


def test_downgrade_base_removes_both_tables(
    alembic_cfg: Config, tmp_path: Path,
) -> None:
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    engine = create_engine(os.environ["DATABASE_URL"])
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "transactions" not in tables
    assert "transactions_audit_log" not in tables


def test_migration_columns_match_models(alembic_cfg: Config) -> None:
    """The migration must produce the exact columns the ORM expects.

    Catches drift when somebody adds a column to ``models.py`` but
    forgets to write the follow-up migration.
    """
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(os.environ["DATABASE_URL"])
    insp = inspect(engine)
    for table in (Transaction.__table__, AuditLog.__table__):
        live_cols = {c["name"] for c in insp.get_columns(table.name)}
        model_cols = {c.name for c in table.columns}
        missing = model_cols - live_cols
        extra = live_cols - model_cols
        assert not missing, (
            f"{table.name}: model declares columns the migration "
            f"didn't create: {missing}"
        )
        assert not extra, (
            f"{table.name}: migration created columns the model "
            f"doesn't know about: {extra}"
        )


def test_metadata_create_all_and_alembic_produce_same_tables(
    tmp_path: Path,
) -> None:
    """``Base.metadata.create_all`` (used by ``--init-postgres``) and
    ``alembic upgrade head`` must produce the same table list.  If
    they diverge, ops will see different shapes depending on which
    bootstrap path they chose."""
    create_all_db = tmp_path / "create_all.db"
    create_all_engine = create_engine(f"sqlite:///{create_all_db}")
    Base.metadata.create_all(create_all_engine)
    create_all_tables = set(inspect(create_all_engine).get_table_names())

    alembic_db = tmp_path / "alembic.db"
    cfg = Config(
        str(Path(__file__).resolve().parent.parent / "alembic.ini"),
    )
    cfg.set_main_option(
        "script_location",
        str(
            Path(__file__).resolve().parent.parent
            / "src" / "expense_tracker" / "ledger" / "nocodb" / "migrations",
        ),
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{alembic_db}")
    command.upgrade(cfg, "head")
    alembic_tables = set(inspect(create_engine(f"sqlite:///{alembic_db}")).get_table_names())
    # Alembic adds its own bookkeeping table — strip before comparing.
    alembic_tables.discard("alembic_version")
    assert create_all_tables == alembic_tables
