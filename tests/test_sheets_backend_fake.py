"""Unit tests for ``FakeSheetsBackend`` and the A1 helpers."""

from __future__ import annotations

import pytest

from expense_tracker.ledger.sheets import (
    CellFormat,
    ConditionalBand,
    FakeSheetsBackend,
    SheetsAlreadyExistsError,
    SheetsNotFoundError,
    col_index_to_letter,
    col_letter_to_index,
)
from expense_tracker.ledger.sheets.backend import (
    _FakeWorksheet,
    parse_a1_cell,
    parse_a1_range,
)

# ─── A1 helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "letter,expected",
    [("A", 0), ("B", 1), ("Z", 25), ("AA", 26), ("AZ", 51), ("BA", 52)],
)
def test_col_letter_to_index(letter, expected):
    assert col_letter_to_index(letter) == expected


@pytest.mark.parametrize(
    "index,expected",
    [(0, "A"), (1, "B"), (25, "Z"), (26, "AA"), (51, "AZ"), (52, "BA")],
)
def test_col_index_to_letter(index, expected):
    assert col_index_to_letter(index) == expected


def test_letter_index_round_trip():
    for i in range(0, 1000):
        assert col_letter_to_index(col_index_to_letter(i)) == i


def test_invalid_letter_raises():
    with pytest.raises(ValueError):
        col_letter_to_index("a1")


def test_negative_index_raises():
    with pytest.raises(ValueError):
        col_index_to_letter(-1)


def test_parse_a1_cell():
    assert parse_a1_cell("A1") == (0, 0)
    assert parse_a1_cell("B3") == (2, 1)
    assert parse_a1_cell("AA10") == (9, 26)


def test_parse_a1_cell_invalid():
    with pytest.raises(ValueError):
        parse_a1_cell("1A")


def test_parse_a1_range():
    assert parse_a1_range("A1:C3") == ((0, 0), (2, 2))
    assert parse_a1_range("Sheet1!A1:B2") == ((0, 0), (1, 1))


def test_parse_a1_range_single_cell():
    assert parse_a1_range("D5") == ((4, 3), (4, 3))


def test_parse_a1_range_open_ended():
    # "A2:M" means "row 2 to the end of the sheet, columns A through M".
    # parse_a1_range returns r2=-1 to signal "use the worksheet's own
    # row_count". The Transactions tab's month-band conditional format
    # uses exactly this shape; without it the gspread backend crashes
    # on the very first row write.
    assert parse_a1_range("A2:M") == ((1, 0), (-1, 12))
    assert parse_a1_range("Sheet1!H2:H") == ((1, 7), (-1, 7))


# ─── Worksheet CRUD ────────────────────────────────────────────────────

def test_create_and_list_worksheets():
    b = FakeSheetsBackend()
    assert b.list_worksheets() == []
    b.create_worksheet("Sheet1")
    b.create_worksheet("Sheet2")
    titles = [w.title for w in b.list_worksheets()]
    assert titles == ["Sheet1", "Sheet2"]


def test_has_worksheet_and_get_worksheet():
    b = FakeSheetsBackend()
    b.create_worksheet("X")
    assert b.has_worksheet("X")
    assert not b.has_worksheet("Y")
    assert b.get_worksheet("X").title == "X"


def test_get_missing_worksheet_raises():
    b = FakeSheetsBackend()
    with pytest.raises(SheetsNotFoundError):
        b.get_worksheet("nope")


def test_create_existing_raises():
    b = FakeSheetsBackend()
    b.create_worksheet("X")
    with pytest.raises(SheetsAlreadyExistsError):
        b.create_worksheet("X")


def test_delete_worksheet():
    b = FakeSheetsBackend()
    b.create_worksheet("X")
    b.create_worksheet("Y")
    b.delete_worksheet("X")
    assert [w.title for w in b.list_worksheets()] == ["Y"]


def test_delete_missing_raises():
    b = FakeSheetsBackend()
    with pytest.raises(SheetsNotFoundError):
        b.delete_worksheet("nope")


def test_rename_worksheet():
    b = FakeSheetsBackend()
    b.create_worksheet("Old")
    b.rename_worksheet("Old", "New")
    assert not b.has_worksheet("Old")
    assert b.has_worksheet("New")


def test_rename_to_self_is_noop():
    b = FakeSheetsBackend()
    b.create_worksheet("X")
    b.rename_worksheet("X", "X")  # should not raise
    assert b.has_worksheet("X")


def test_rename_to_existing_raises():
    b = FakeSheetsBackend()
    b.create_worksheet("X")
    b.create_worksheet("Y")
    with pytest.raises(SheetsAlreadyExistsError):
        b.rename_worksheet("X", "Y")


# ─── Worksheet read/write ──────────────────────────────────────────────

def test_update_and_read_values():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.update_values("A1:B2", [["a", "b"], ["c", "d"]])
    assert ws.get_values("A1:B2") == [["a", "b"], ["c", "d"]]


def test_update_oversized_raises():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    with pytest.raises(ValueError):
        ws.update_values("A1:A1", [["a"], ["b"]])


def test_get_unwritten_returns_blank():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    # _FakeWorksheet implements .cell() helper
    assert isinstance(ws, _FakeWorksheet)
    assert ws.cell("Z99") == ""


def test_append_rows():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.update_values("A1:C1", [["h1", "h2", "h3"]])
    ws.append_rows([[1, 2, 3], [4, 5, 6]])
    assert isinstance(ws, _FakeWorksheet)
    assert ws.cell("A2") == 1
    assert ws.cell("B3") == 5


def test_clear_resets_state():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.update_values("A1", [["hi"]])
    ws.format_range("A1", CellFormat(bold=True))
    ws.clear()
    assert isinstance(ws, _FakeWorksheet)
    assert ws.cell("A1") == ""
    assert ws.format_calls() == []


# ─── Formatting + freeze + widths + bands ──────────────────────────────

def test_format_range_logged():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.format_range("A1:B2", CellFormat(bold=True, background_color="#FFF"))
    assert isinstance(ws, _FakeWorksheet)
    calls = ws.format_calls()
    assert len(calls) == 1
    assert calls[0][0] == "A1:B2"
    assert calls[0][1].bold is True


def test_format_range_empty_is_noop():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.format_range("A1", CellFormat())  # all None
    assert isinstance(ws, _FakeWorksheet)
    assert ws.format_calls() == []


def test_freeze_state():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.freeze(rows=2, cols=1)
    assert isinstance(ws, _FakeWorksheet)
    assert ws.freeze_state == (2, 1)


def test_set_column_widths_starts_at_letter():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    ws.set_column_widths_px(start_col="B", widths=[100, 150, 200])
    assert isinstance(ws, _FakeWorksheet)
    assert ws.column_widths == {"B": 100, "C": 150, "D": 200}


def test_set_hidden_round_trip():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    assert ws.hidden is False
    ws.set_hidden(True)
    assert ws.hidden is True
    ws.set_hidden(False)
    assert ws.hidden is False


def test_add_conditional_band_recorded():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X")
    band = ConditionalBand(
        range_a1="A2:Z100",
        predicate_formula="=ISEVEN(MONTH($B2))",
        background_color="#F2F2F2",
    )
    ws.add_conditional_band(band)
    assert isinstance(ws, _FakeWorksheet)
    assert ws.conditional_bands == [band]


def test_resize_updates_dimensions():
    b = FakeSheetsBackend()
    ws = b.create_worksheet("X", rows=10, cols=5)
    ws.resize(rows=20)
    assert ws.row_count == 20
    assert ws.col_count == 5
    ws.resize(cols=10)
    assert ws.col_count == 10
