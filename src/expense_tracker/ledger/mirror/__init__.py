"""Mirror edition of :class:`LedgerBackend`.

Wraps two backends — a *primary* and a *secondary* — and forwards
every write to both so the user gets dual storage with one chat
message:

* **Primary**  (defaults to Sheets) — authoritative for reads, must
  succeed on writes.  Failures bubble up to the user.
* **Secondary** (defaults to Postgres / NocoDB) — best-effort mirror.
  Failures are logged at WARNING and swallowed so a Supabase blip
  never breaks the user's chat flow.

Reads are served from the primary only.  Use ``expense --reconcile``
to detect + back-fill any rows the secondary missed during outages.

Why "primary first, fail-soft secondary"?
-----------------------------------------

A personal bot fires maybe 10-50 writes/day.  If both stores had to
succeed for the user to get a confirmation, every Supabase free-tier
hiccup would surface as a failed expense.  Worse, if the user retried
they'd get two copies in Sheets and one in Postgres — the classic
"I clicked submit twice" data-quality problem.

By making the secondary write best-effort:

* The user's chat experience is **identical** to the Sheets-only
  edition (same latency, same failure surface).
* Postgres becomes a passive accumulator that "catches up" on every
  write.
* Drift recovery is a one-shot CLI command, not a runtime concern.
"""

from .adapter import MirrorLedgerBackend
from .reconcile import ReconcileReport, reconcile

__all__ = ["MirrorLedgerBackend", "ReconcileReport", "reconcile"]
