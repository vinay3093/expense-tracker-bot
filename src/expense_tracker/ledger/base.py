"""Backend-agnostic ledger contract.

This module defines the **storage abstraction** that the chat pipeline
talks to.  Every storage edition (Sheets, Postgres, ...) implements
the :class:`LedgerBackend` Protocol declared here, and every other
package in the codebase imports the data shapes from this module — *not*
from any edition-specific module.

Why split this out
------------------

The bot has two equally-valid "homes" for the user's transaction
ledger:

* **Sheets edition** — the spreadsheet *is* the database.  Nice for
  zero-setup, easy manual editing, and a familiar interface.
* **Postgres edition** — a typed SQL ledger with a NocoDB UI on top.
  Better for >10k rows, multi-device usage, and richer queries.

The chat pipeline (extractor → logger → reply) shouldn't care which
one is active.  By depending on this Protocol, the same
:class:`~expense_tracker.pipeline.logger.ExpenseLogger`,
:class:`~expense_tracker.pipeline.correction.CorrectionLogger`, and
:class:`~expense_tracker.pipeline.retrieval.RetrievalEngine` work
unchanged against either edition.

Design notes
------------

* All data shapes here are **frozen dataclasses** so callers can hash
  them, compare them, and pass them across thread / process boundaries
  without surprise mutations.
* :class:`LedgerRow` deliberately carries a ``row_index: int`` field.
  In the Sheets edition that's the 1-based spreadsheet row number; in
  the Postgres edition it's the ``SERIAL`` primary key.  Either way
  it's a stable monotonic ID the caller can use for sorting + ``/undo``.
* :class:`LastRow` returns a ``values`` dict keyed by the canonical
  column key (``"category"``, ``"amount_usd"``, ...) rather than a
  positional list — the positional layout is a Sheets-only concept.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# ─── Errors ─────────────────────────────────────────────────────────────


class LedgerError(Exception):
    """Base class for any storage-layer failure surfaced to the pipeline.

    The Sheets edition raises :class:`SheetsError` (a subclass) for API
    failures; the Postgres edition raises :class:`PostgresLedgerError`.
    Pipeline-level wrappers (``ExpenseLogError``, ``CorrectionError``,
    ``RetrievalError``) all catch :class:`LedgerError` so adding a new
    backend doesn't require touching the chat layer.
    """


# ─── Universal data shapes ──────────────────────────────────────────────


@dataclass(frozen=True)
class TransactionRow:
    """One row about to be written to the master ledger.

    Constructed by the chat pipeline before storage; the backend turns
    it into whatever its native row type is (a Sheets cell-list or a
    SQLAlchemy model instance).

    Field set is **frozen by contract** — adding a column requires a
    migration plan in *every* backend, so we want a code review before
    that happens.

    Money fields
    ~~~~~~~~~~~~
    ``amount`` is the original amount the user logged (in
    ``currency``); ``amount_usd`` is the converted-to-primary value
    that monthly + YTD aggregates sum.  ``fx_rate`` is the rate that
    was applied (``1.0`` when no conversion needed).

    Time fields
    ~~~~~~~~~~~
    ``date`` is the day the expense was *incurred*; ``timestamp`` is
    when the bot *wrote* it.  The gap between them is the "logged
    late by N days" signal that ledger inspectors care about.
    """

    date: date_cls
    day: str
    """Short weekday name in en-US (``"Mon"``, ``"Tue"``, ...)."""
    month: str
    """Full month name in en-US (``"April"``)."""
    year: int
    category: str
    note: str | None
    vendor: str | None
    amount: float
    currency: str
    amount_usd: float
    fx_rate: float
    source: str = "chat"
    """How the row entered the system: ``"chat"``, ``"cli"``, ``"manual"``."""
    trace_id: str | None = None
    """LLM call ID that produced this row, for audit + debugging."""
    timestamp: datetime | None = None


@dataclass(frozen=True)
class LedgerRow:
    """One parsed row read *back* from the master ledger.

    Distinct from :class:`TransactionRow` (write-side) so each side
    can evolve its own auxiliary fields:

    * Read rows carry a ``row_index`` (stable backend-assigned ID).
    * Optional cells (``note``, ``vendor``, ``trace_id``,
      ``timestamp``) parse to ``None`` when blank rather than empty
      strings — easier downstream formatting.
    """

    row_index: int
    """Backend-assigned monotonic ID.  Sheets: 1-based row number;
    Postgres: ``SERIAL`` primary key."""

    date: date_cls
    day: str
    month: str
    year: int
    category: str
    note: str | None
    vendor: str | None
    amount: float
    currency: str
    amount_usd: float
    fx_rate: float
    source: str
    trace_id: str | None
    timestamp: datetime | None


@dataclass(frozen=True)
class SkippedRow:
    """One ledger row the parser couldn't turn into a :class:`LedgerRow`.

    Surfaced by :meth:`LedgerBackend.read_all` and the
    ``--inspect-ledger`` CLI command so the operator can locate + fix
    the offending row.  Counted (but not detailed) on every retrieval
    query so the chat layer can mention "1 row skipped due to bad
    formatting" without dragging the parse-error string into every
    reply.
    """

    row_index: int
    """Backend-assigned ID matching what the storage UI shows."""

    reason: str
    """Human-readable parse-failure reason."""

    raw_values: list[str]
    """Raw cell values for the failed row, as strings, for diagnostics."""


@dataclass(frozen=True)
class LedgerInspection:
    """Full parse report of the master ledger.

    Returned by :meth:`LedgerBackend.read_all` (when the caller asks
    for skipped detail) and by the ``--inspect-ledger`` CLI command.
    """

    sheet_name: str
    """Backend-specific identifier of the source store.  For Sheets
    it's the tab name (``"Transactions"``); for Postgres it's the
    table name (``"transactions"``)."""

    parsed: list[LedgerRow]
    skipped: list[SkippedRow]

    @property
    def total_rows(self) -> int:
        return len(self.parsed) + len(self.skipped)


@dataclass(frozen=True)
class LastRow:
    """Snapshot of the most-recently-written transaction.

    Used by the ``/undo`` and ``/edit`` flows.  ``is_empty`` means
    the ledger has zero transactions.  ``values`` is a dict keyed by
    the canonical column key so callers can do ``snap.value("category")``
    without knowing the underlying storage layout.
    """

    is_empty: bool
    row_index: int | None
    """Backend-assigned ID of the snapshotted row.  ``None`` iff
    ``is_empty == True``."""

    values: dict[str, Any] = field(default_factory=dict)
    """Field map: column key → cell value (already coerced to its
    natural Python type where the backend can; raw string otherwise)."""

    def value(self, key: str) -> Any:
        """Project the snapshot to a column by its schema key.

        Returns ``None`` when the snapshot is empty or the key is
        missing — never raises.  This mirrors the lenient reading
        style the chat layer already expects.
        """
        if self.is_empty:
            return None
        return self.values.get(key)


@dataclass(frozen=True)
class BackendHealth:
    """Result of a cheap connectivity ping against the storage backend.

    Surfaced by the (planned) ``expense --healthcheck`` CLI command.
    The Telegram ``/healthcheck`` command will use the same shape.
    """

    ok: bool
    backend: str
    """Backend name: ``"sheets"`` or ``"postgres"``."""

    latency_ms: float
    detail: str
    """Free-form: usually the spreadsheet title or the Postgres server version."""


@dataclass(frozen=True)
class PeriodInfo:
    """Result of :meth:`LedgerBackend.ensure_period`.

    For the Sheets edition this carries the monthly summary tab name
    (``"April 2026"``) and a flag indicating whether the call had to
    create it.  For the Postgres edition both fields are ``None`` /
    ``False`` because there's nothing per-month to provision.
    """

    name: str | None
    """Storage-specific name of the period container (sheets tab name).
    ``None`` when the backend has no per-period concept."""

    created: bool
    """True iff this call provisioned the period container.
    Used by the chat reply layer to say "I set up May 2026 for you"."""


# ─── The Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class LedgerBackend(Protocol):
    """Where transactions actually live.

    Every method on this Protocol is **called by the chat pipeline**.
    Edition-specific concerns (Google Sheets formula nudges, Postgres
    connection pools) stay inside the implementation and never bubble
    up here.

    Implementations may raise :class:`LedgerError` (or any subclass)
    on storage failures.  Empty ledgers are *not* an error — they
    return empty results.
    """

    # ─── Identity ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Short backend identifier: ``"sheets"`` / ``"postgres"``."""
        ...

    @property
    def transactions_label(self) -> str:
        """Human-readable name of the master ledger destination.

        * Sheets edition: ``"Transactions"`` (the tab name).
        * Postgres edition: ``"transactions"`` (the table name).

        Used by the chat reply layer + audit logs so log messages look
        the same regardless of backend ("Logged to Transactions" /
        "Logged to transactions").
        """
        ...

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def health_check(self) -> BackendHealth:
        """Cheap connectivity ping.  Should not raise on transient
        network issues — return ``ok=False`` with a useful ``detail``
        instead.  Raising is reserved for misconfiguration (missing
        credentials, etc.)."""
        ...

    def init_storage(self) -> None:
        """Create whatever the backend needs to accept writes.

        Sheets edition: create the ``Transactions`` tab if missing.
        Postgres edition: ``CREATE TABLE`` if missing (handled by
        Alembic in production; this method is the test-friendly entry
        point).

        Idempotent: safe to call on every process start.
        """
        ...

    def ensure_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> PeriodInfo:
        """Make sure the storage is ready for writes covering
        ``(year, month)``.

        Sheets edition: provisions the monthly summary tab + formula
        grid.  Postgres edition: no-op (single ``transactions`` table
        partitioned by date — nothing to create).
        """
        ...

    # ─── Write side ────────────────────────────────────────────────────

    def append(self, rows: Sequence[TransactionRow]) -> list[int]:
        """Append one or more rows.  Returns the assigned row IDs in
        the same order.  Atomic per call: either all rows land or
        none do.
        """
        ...

    def recompute_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> str | None:
        """Trigger any post-write recomputation.

        Sheets edition: re-asserts the monthly tab's headline formulas
        to bust Google's stale formula cache.  Returns the human
        period name (``"April 2026"``) on success, ``None`` if there
        was nothing to recompute.  Failure is logged + swallowed —
        recomputation is a UX concern, never a correctness one.

        Postgres edition: no-op, returns ``None``.
        """
        ...

    # ─── Read side ─────────────────────────────────────────────────────

    def read_all(
        self,
        *,
        collect_skipped_detail: bool = False,
    ) -> LedgerInspection:
        """Return every parsed transaction, plus any unparseable rows.

        ``collect_skipped_detail=False`` (the hot retrieval path) keeps
        the ``skipped`` list empty but still reports an accurate
        ``len(skipped) == count``.  Setting it to ``True`` populates
        the parse-error reasons + raw values for diagnostics.
        """
        ...

    # ─── Last-row operations (for /undo and /edit) ─────────────────────

    def get_last(self) -> LastRow:
        """Return the most-recently-written transaction (or empty).
        Side-effect-free.
        """
        ...

    def delete_last(self) -> LastRow:
        """Delete the most-recent transaction; return its pre-delete
        snapshot.  ``is_empty == True`` snapshots mean nothing was
        deleted (empty ledger).
        """
        ...

    def update_last(self, updates: dict[str, Any]) -> LastRow:
        """Patch named fields on the most-recent transaction.

        ``updates`` is keyed by canonical column key
        (``"category"``, ``"amount"``, ``"amount_usd"``, ``"fx_rate"``).
        Returns the **pre-edit** snapshot so the chat layer can render
        a diff.  Empty-ledger calls are a no-op that returns an
        ``is_empty`` snapshot.
        """
        ...


__all__ = [
    "BackendHealth",
    "LastRow",
    "LedgerBackend",
    "LedgerError",
    "LedgerInspection",
    "LedgerRow",
    "PeriodInfo",
    "SkippedRow",
    "TransactionRow",
]
