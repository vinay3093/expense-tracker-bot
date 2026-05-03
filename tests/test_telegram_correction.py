"""Tests for CorrectionProcessor — the /last, /undo, /edit Telegram glue.

We test the pure-Python orchestrator with a stub :class:`CorrectionLogger`
substitute. The Telegram SDK isn't exercised here; the SDK glue
(``make_undo_handler``, etc) lives in ``test_telegram_handlers.py``.

Why a stub instead of a real CorrectionLogger? Two reasons:

1. The real one needs a Sheets backend + FX cache. We've already
   covered its semantics in ``test_pipeline_correction.py``; here we
   want to assert the *processor's* behavior — auth gating, error
   formatting, command parsing.
2. We need to simulate failure paths (CorrectionError raises, missing
   logger) deterministically, which is awkward to set up against a
   real :class:`FakeSheetsBackend`.
"""

from __future__ import annotations

import pytest

from expense_tracker.ledger.sheets.transactions import LastRow
from expense_tracker.pipeline.correction import (
    CorrectionError,
    EditResult,
    UndoResult,
)
from expense_tracker.telegram_app.auth import Authorizer
from expense_tracker.telegram_app.bot import (
    CorrectionProcessor,
    _parse_edit_args,
)

# ─── Test doubles ──────────────────────────────────────────────────────


class _StubCorrector:
    """Minimal stand-in for :class:`CorrectionLogger`.

    Records each call so tests can assert the processor only invokes
    the underlying logger when allowed.
    """

    def __init__(
        self,
        *,
        peek: LastRow | None = None,
        undo_result: UndoResult | None = None,
        edit_result: EditResult | None = None,
        peek_raises: Exception | None = None,
        undo_raises: Exception | None = None,
        edit_raises: Exception | None = None,
    ) -> None:
        self._peek = peek
        self._undo = undo_result
        self._edit = edit_result
        self._peek_raises = peek_raises
        self._undo_raises = undo_raises
        self._edit_raises = edit_raises
        self.peek_calls = 0
        self.undo_calls = 0
        self.edit_calls: list[dict] = []

    def peek_last(self) -> LastRow:
        self.peek_calls += 1
        if self._peek_raises is not None:
            raise self._peek_raises
        assert self._peek is not None, "peek not configured on stub"
        return self._peek

    def undo(self) -> UndoResult:
        self.undo_calls += 1
        if self._undo_raises is not None:
            raise self._undo_raises
        assert self._undo is not None, "undo not configured on stub"
        return self._undo

    def edit(self, *, amount=None, category=None) -> EditResult:
        self.edit_calls.append({"amount": amount, "category": category})
        if self._edit_raises is not None:
            raise self._edit_raises
        assert self._edit is not None, "edit not configured on stub"
        return self._edit


def _row(values: dict[str, object]) -> LastRow:
    """Build a :class:`LastRow` from a key->value mapping.

    Maps schema keys into the right positional slots so ``snap.value()``
    returns what tests expect.
    """
    from expense_tracker.ledger.sheets.transactions import (
        TRANSACTIONS_COLUMNS,
        index_for,
    )

    cells: list[object] = ["" for _ in TRANSACTIONS_COLUMNS]
    for k, v in values.items():
        cells[index_for(k)] = v
    return LastRow(row_index=2, values=cells)


def _processor(
    *,
    allowed_ids: set[int],
    corrector: _StubCorrector | None,
) -> CorrectionProcessor:
    return CorrectionProcessor(
        authorizer=Authorizer(frozenset(allowed_ids)),
        corrector=corrector,  # type: ignore[arg-type]
    )


# ─── Auth gating ───────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["last", "undo"])
def test_unauthorized_users_never_hit_corrector(verb):
    stub = _StubCorrector(peek=_row({"category": "Food"}))
    proc = _processor(allowed_ids={42}, corrector=stub)

    method = getattr(proc, f"process_{verb}")
    reply = method(user_id=7)

    assert "not authorized" in reply.lower()
    assert "7" in reply  # echo user id so the operator can allow-list
    assert stub.peek_calls == 0
    assert stub.undo_calls == 0


def test_unauthorized_user_for_edit_skips_logger():
    stub = _StubCorrector()
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=99, args_text="amount 50")

    assert "not authorized" in reply.lower()
    assert stub.edit_calls == []


# ─── /last ─────────────────────────────────────────────────────────────


def test_last_renders_pretty_summary_for_authorized_user():
    snap = _row(
        {
            "date": "2026-04-25",
            "day": "Sat",
            "category": "Saloon",
            "amount": 30.0,
            "currency": "USD",
            "amount_usd": 30.0,
            "note": "haircut",
        }
    )
    stub = _StubCorrector(peek=snap)
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_last(user_id=42)
    assert "Last logged expense" in reply
    assert "Saloon" in reply
    assert "30.0" in reply
    assert "haircut" in reply
    assert stub.peek_calls == 1


def test_last_handles_empty_ledger_gracefully():
    stub = _StubCorrector(peek=LastRow(row_index=None, values=[]))
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_last(user_id=42)
    assert "empty" in reply.lower()


def test_last_handles_corrector_error():
    stub = _StubCorrector(peek_raises=CorrectionError("sheets timeout"))
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_last(user_id=42)
    assert "couldn't apply" in reply.lower()
    assert "sheets timeout" in reply


def test_missing_corrector_yields_friendly_reply():
    proc = _processor(allowed_ids={42}, corrector=None)
    reply = proc.process_last(user_id=42)
    assert "configured" in reply.lower()
    assert "telegram" in reply.lower()


# ─── /undo ─────────────────────────────────────────────────────────────


def test_undo_reports_deletion_and_recompute_status():
    deleted = _row(
        {
            "date": "2026-04-25",
            "category": "Saloon",
            "amount": 30.0,
            "currency": "USD",
        }
    )
    result = UndoResult(
        deleted_row=deleted,
        transactions_tab="Transactions",
        monthly_tab="April 2026",
        monthly_tab_recomputed=True,
    )
    stub = _StubCorrector(undo_result=result)
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_undo(user_id=42)
    assert "Deleted last expense" in reply
    assert "Saloon" in reply
    assert "April 2026" in reply
    assert stub.undo_calls == 1


def test_undo_on_empty_ledger_replies_friendly():
    result = UndoResult(
        deleted_row=LastRow(row_index=None, values=[]),
        transactions_tab="Transactions",
        monthly_tab=None,
        monthly_tab_recomputed=False,
    )
    stub = _StubCorrector(undo_result=result)
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_undo(user_id=42)
    assert "empty" in reply.lower()


def test_undo_corrector_error_surfaces_in_reply():
    stub = _StubCorrector(undo_raises=CorrectionError("API quota"))
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_undo(user_id=42)
    assert "couldn't apply" in reply.lower()
    assert "API quota" in reply


# ─── /edit ─────────────────────────────────────────────────────────────


def test_edit_amount_calls_corrector_with_parsed_value():
    before = _row(
        {
            "category": "Saloon",
            "amount": 30.0,
            "currency": "USD",
        }
    )
    result = EditResult(
        before=before,
        applied={"amount": 50.0, "amount_usd": 50.0, "fx_rate": 1.0},
        transactions_tab="Transactions",
        monthly_tab="April 2026",
        monthly_tab_recomputed=True,
    )
    stub = _StubCorrector(edit_result=result)
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="amount 50")

    assert stub.edit_calls == [{"amount": 50.0, "category": None}]
    assert "Updated last expense" in reply
    assert "30.0" in reply  # before
    assert "50.0" in reply  # after
    assert "April 2026" in reply  # nudge call-out


def test_edit_category_passes_raw_string_to_corrector():
    before = _row({"category": "Saloon"})
    result = EditResult(
        before=before,
        applied={"category": "Shopping"},
        transactions_tab="Transactions",
        monthly_tab="April 2026",
        monthly_tab_recomputed=True,
    )
    stub = _StubCorrector(edit_result=result)
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="category Shopping")
    assert stub.edit_calls == [{"amount": None, "category": "Shopping"}]
    assert "Saloon" in reply
    assert "Shopping" in reply


def test_edit_category_supports_multi_word_value():
    """Category names like "India Expense" must round-trip intact."""
    before = _row({"category": "Misc"})
    result = EditResult(
        before=before,
        applied={"category": "India Expense"},
        transactions_tab="Transactions",
        monthly_tab="April 2026",
        monthly_tab_recomputed=False,
    )
    stub = _StubCorrector(edit_result=result)
    proc = _processor(allowed_ids={42}, corrector=stub)

    proc.process_edit(user_id=42, args_text="category India Expense")
    assert stub.edit_calls[-1]["category"] == "India Expense"


def test_edit_with_no_args_returns_usage_help():
    stub = _StubCorrector()
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="")
    assert "Usage" in reply
    assert "amount" in reply
    assert "category" in reply
    assert stub.edit_calls == []


def test_edit_with_unknown_subcommand_returns_usage():
    stub = _StubCorrector()
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="vendor Starbucks")
    assert "Usage" in reply
    assert stub.edit_calls == []


def test_edit_with_unparseable_amount_replies_clearly():
    stub = _StubCorrector()
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="amount potato")
    assert "potato" in reply
    assert "number" in reply.lower()
    assert stub.edit_calls == []


def test_edit_with_zero_amount_rejected_before_logger():
    stub = _StubCorrector()
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="amount 0")
    assert "positive" in reply.lower()
    assert stub.edit_calls == []


def test_edit_corrector_error_surfaces_in_reply():
    stub = _StubCorrector(edit_raises=CorrectionError("FX rate unavailable"))
    proc = _processor(allowed_ids={42}, corrector=stub)

    reply = proc.process_edit(user_id=42, args_text="amount 100")
    assert "couldn't apply" in reply.lower()
    assert "FX rate unavailable" in reply


# ─── _parse_edit_args (direct) ─────────────────────────────────────────


def test_parse_edit_args_amount_simple():
    assert _parse_edit_args("amount 50") == (50.0, None)


def test_parse_edit_args_amount_decimal():
    assert _parse_edit_args("amount 12.50") == (12.5, None)


def test_parse_edit_args_amount_with_trailing_token_ignored():
    # Trailing currency token is currently parsed-and-ignored; we
    # always re-use the row's original currency.
    assert _parse_edit_args("amount 50 INR") == (50.0, None)


def test_parse_edit_args_category_single_word():
    assert _parse_edit_args("category Food") == (None, "Food")


def test_parse_edit_args_category_multi_word():
    assert _parse_edit_args("category India Expense") == (None, "India Expense")


def test_parse_edit_args_blank_returns_usage_string():
    out = _parse_edit_args("")
    assert isinstance(out, str)
    assert "Usage" in out


def test_parse_edit_args_unknown_returns_usage_string():
    out = _parse_edit_args("vendor Starbucks")
    assert isinstance(out, str)
    assert "Usage" in out


def test_parse_edit_args_amount_unparseable_returns_error_string():
    out = _parse_edit_args("amount banana")
    assert isinstance(out, str)
    assert "banana" in out


def test_parse_edit_args_amount_negative_returns_error_string():
    out = _parse_edit_args("amount -5")
    assert isinstance(out, str)
    assert "positive" in out
