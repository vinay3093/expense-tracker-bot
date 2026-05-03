"""Ledger storage layer — where the expense ledger physically lives.

This package isolates *where data is stored* from the rest of the
codebase.  Two editions live side-by-side under this package:

* :mod:`expense_tracker.ledger.sheets`  — Google Sheets edition
  (mirrors a manual monthly spreadsheet, with formula-driven monthly
  + YTD tabs, multi-currency conversion, etc.).
* :mod:`expense_tracker.ledger.nocodb`  — Postgres + NocoDB edition
  (typed SQL backend with a NocoDB UI on top — added in Step 10b).

Both editions implement the same :class:`LedgerBackend` Protocol
(:mod:`expense_tracker.ledger.base`), so the chat pipeline, Telegram
bot, and CLI never need to know which one is active.  The choice
flows from a single env var: ``STORAGE_BACKEND=sheets|nocodb``.

For now (Step 10a — folder rearrangement only) only the Sheets
edition is wired up.  All public symbols still live where the older
imports expect them; the Protocol + factory are layered on in Step
10b without breaking anything.
"""

__all__: list[str] = []
