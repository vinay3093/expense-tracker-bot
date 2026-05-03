"""Mirror :class:`LedgerBackend` — primary first, fail-soft secondary.

Wraps two backends (typically Sheets + Postgres) so every chat
write ends up in both stores with one user-visible call.

Behaviour summary
-----------------

| Operation         | Primary | Secondary |
|-------------------|---------|-----------|
| ``init_storage``  | required| best-effort |
| ``ensure_period`` | required| best-effort |
| ``append``        | required| best-effort (uses primary's row IDs back to caller) |
| ``recompute_period`` | required (return value) | best-effort |
| ``read_all``      | primary only | — |
| ``get_last``      | primary only | — |
| ``delete_last``   | required (return value) | best-effort, deletes its own "last" |
| ``update_last``   | required (return value) | best-effort, updates its own "last" |
| ``health_check``  | always returns primary | secondary status logged |

"Best-effort" means: caught + logged at WARNING level, never raised.
The user always sees the primary's reply.

Why not row-id-aware mirroring?
-------------------------------

Sheets row indices and Postgres ``SERIAL`` IDs are independent
sequences — they will diverge as soon as the secondary misses an
event (or as soon as Postgres soft-deletes don't shift IDs the way
Sheets row deletes do).  Trying to keep them aligned would require
either a translation table or a mirror-specific schema change.

Instead, both backends maintain their own *logical* ordering:

* :meth:`delete_last` deletes whichever row each backend considers
  "most recent."  Provided neither has drifted, that's the same
  expense in both stores.
* :meth:`update_last` updates whichever row each backend considers
  "most recent" with the same field map.

Drift between the two is detected + repaired by
:func:`expense_tracker.ledger.mirror.reconcile.reconcile`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from ..base import (
    BackendHealth,
    LastRow,
    LedgerBackend,
    LedgerError,
    LedgerInspection,
    PeriodInfo,
    TransactionRow,
)

_log = logging.getLogger(__name__)


class MirrorLedgerBackend:
    """Forward every call to a primary + secondary backend.

    Writes are sent to the primary first; on success they're sent to
    the secondary as a best-effort follow-up.  Reads always come from
    the primary so the chat layer's invariants (row indices, "last
    row" semantics, ledger inspection counts) are unchanged from a
    pure single-backend setup.
    """

    name = "mirror"

    def __init__(
        self,
        *,
        primary: LedgerBackend,
        secondary: LedgerBackend,
    ) -> None:
        if primary is secondary:
            raise ValueError(
                "MirrorLedgerBackend.primary and .secondary must be "
                "distinct backends.  Got the same instance for both."
            )
        self._primary = primary
        self._secondary = secondary

    # ─── Identity ──────────────────────────────────────────────────────

    @property
    def transactions_label(self) -> str:
        # Label follows the primary so chat replies and the audit log
        # keep using the name the user already sees on their phone
        # ("Logged to Transactions").
        return self._primary.transactions_label

    @property
    def primary(self) -> LedgerBackend:
        """Underlying authoritative backend (used for reads)."""
        return self._primary

    @property
    def secondary(self) -> LedgerBackend:
        """Underlying mirror backend (best-effort, used for back-fill)."""
        return self._secondary

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def health_check(self) -> BackendHealth:
        """Return the *primary* health.  Log secondary status separately.

        Returning a single BackendHealth keeps the existing
        ``--healthcheck`` CLI surface unchanged.  A future "mirror
        healthcheck" CLI flag can probe both individually if/when
        we want richer reporting.
        """
        primary_health = self._primary.health_check()
        secondary_health = self._safe_secondary(
            "health_check", lambda: self._secondary.health_check()
        )
        if secondary_health is None or not secondary_health.ok:
            _log.warning(
                "mirror: secondary backend %s is unhealthy: %s",
                self._secondary.name,
                "exception (see prior log)" if secondary_health is None
                else secondary_health.detail,
            )
        else:
            _log.info(
                "mirror: secondary backend %s healthy (%.0f ms)",
                self._secondary.name, secondary_health.latency_ms,
            )
        return primary_health

    def init_storage(self) -> None:
        """Initialise both backends.  Primary failure raises; secondary
        failure is logged and swallowed so the bot still starts up
        when (e.g.) Postgres is briefly unreachable."""
        self._primary.init_storage()
        self._safe_secondary("init_storage", self._secondary.init_storage)

    def ensure_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> PeriodInfo:
        info = self._primary.ensure_period(
            year=year, month=month, categories=categories,
        )
        self._safe_secondary(
            "ensure_period",
            lambda: self._secondary.ensure_period(
                year=year, month=month, categories=categories,
            ),
        )
        return info

    # ─── Write side ────────────────────────────────────────────────────

    def append(self, rows: Sequence[TransactionRow]) -> list[int]:
        """Append to both stores.  The returned IDs are the *primary's*
        IDs (Sheets row indices) so the chat reply quotes a number the
        user can find on their phone.
        """
        if not rows:
            return []
        primary_ids = self._primary.append(rows)
        # Pass a defensive copy in case the secondary wants to mutate.
        # No-op for the SQLAlchemy / Sheets backends, but cheap insurance.
        self._safe_secondary(
            "append",
            lambda: self._secondary.append(list(rows)),
        )
        return primary_ids

    def recompute_period(
        self,
        *,
        year: int,
        month: int,
        categories: Sequence[str],
    ) -> str | None:
        result = self._primary.recompute_period(
            year=year, month=month, categories=categories,
        )
        self._safe_secondary(
            "recompute_period",
            lambda: self._secondary.recompute_period(
                year=year, month=month, categories=categories,
            ),
        )
        return result

    # ─── Read side ─────────────────────────────────────────────────────

    def read_all(
        self,
        *,
        collect_skipped_detail: bool = False,
    ) -> LedgerInspection:
        """Reads always come from the primary.  Secondary is a write
        sink only — keeping reads single-sourced means retrieval
        results, "last row" semantics, and ledger inspection counts
        all stay deterministic and identical to the primary-only
        edition.
        """
        return self._primary.read_all(
            collect_skipped_detail=collect_skipped_detail,
        )

    # ─── Last-row operations (for /undo and /edit) ─────────────────────

    def get_last(self) -> LastRow:
        """Return the primary's last-row snapshot.

        Side-effect-free — also doesn't touch the secondary, so
        ``/last`` stays a one-API-call command (no Supabase round
        trip just to view the last expense)."""
        return self._primary.get_last()

    def delete_last(self) -> LastRow:
        """Delete the most-recent row from BOTH backends.

        Each backend deletes whichever row IT considers "last" —
        Sheets pops the bottom row, Postgres deletes
        ``MAX(id) WHERE deleted_at IS NULL``.  Provided the two haven't
        drifted (no missed writes during a Supabase outage), they
        reference the same logical expense.

        The returned snapshot is the *primary's* — that's what the
        chat reply quotes.
        """
        snapshot = self._primary.delete_last()
        self._safe_secondary("delete_last", self._secondary.delete_last)
        return snapshot

    def update_last(self, updates: dict[str, Any]) -> LastRow:
        """Patch the most-recent row in BOTH backends with the same
        field map.  Returns the primary's pre-edit snapshot."""
        snapshot = self._primary.update_last(updates)
        self._safe_secondary(
            "update_last",
            lambda: self._secondary.update_last(dict(updates)),
        )
        return snapshot

    # ─── Internals ─────────────────────────────────────────────────────

    def _safe_secondary(self, op: str, fn):  # type: ignore[no-untyped-def]
        """Run ``fn()`` against the secondary, swallowing every error.

        Logs at WARNING with backend name + operation + exception so
        the operator can see drift in their host's log stream and act
        (run ``expense --reconcile``).  Returns the result on success,
        ``None`` on failure.
        """
        start = time.perf_counter()
        try:
            result = fn()
        except LedgerError as exc:
            _log.warning(
                "mirror: secondary backend %s failed during %s "
                "(%.0f ms) — drift may have occurred, run "
                "`expense --reconcile` to repair.  Error: %s",
                self._secondary.name, op,
                (time.perf_counter() - start) * 1000, exc,
            )
            return None
        except Exception as exc:
            # ANY non-LedgerError from the secondary (network blip,
            # SDK bug, permission change) must NOT break the user's
            # chat.  Belt-and-braces.
            _log.warning(
                "mirror: secondary backend %s raised unexpected %s "
                "during %s (%.0f ms) — swallowing, run "
                "`expense --reconcile` to repair.  Error: %s",
                self._secondary.name, type(exc).__name__, op,
                (time.perf_counter() - start) * 1000, exc,
            )
            return None
        return result


__all__ = ["MirrorLedgerBackend"]
