"""Sheets backend protocol + an in-memory fake for tests.

We deliberately decouple the layer that *describes* what the bot wants
to do (set this range to these values; bold this header; freeze two
columns) from the layer that *executes* it (gspread, or, in tests,
:class:`FakeSheetsBackend`). Two reasons:

1. **Offline tests.** Every unit test in this layer runs against the
   fake; no network, no service-account JSON, no flaky API.
2. **Future flexibility.** Swapping in the official ``google-api-python
   -client`` or a hypothetical Excel/CSV backend later is an isolated
   change — Builders never know which one they're talking to.

Cell addresses use A1 notation throughout (``A1``, ``B2:E10``,
``Sheet1!A1:E10``). Formulas are values starting with ``"="``; the
backend is responsible for telling the underlying API to interpret
them as formulas (gspread does this with ``USER_ENTERED``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ─── Formatting model ───────────────────────────────────────────────────

@dataclass(frozen=True)
class CellFormat:
    """Backend-agnostic description of cell-level formatting.

    All fields are optional; ``None`` means "leave whatever was there
    before alone". Keep the surface narrow — anything richer (borders,
    conditional rules, etc.) gets a dedicated backend method.
    """

    background_color: str | None = None
    foreground_color: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    font_size: int | None = None
    horizontal_alignment: str | None = None  # LEFT | CENTER | RIGHT
    vertical_alignment: str | None = None    # TOP  | MIDDLE | BOTTOM
    number_format: str | None = None
    wrap: bool | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in self.__dataclass_fields__.values())


@dataclass(frozen=True)
class ConditionalBand:
    """Conditional-format rule that paints rows matching a predicate.

    Used to give the Transactions tab alternating month bands without
    inserting separator rows that would break SUMIFS formulas.
    """

    range_a1: str
    predicate_formula: str  # e.g. "=ISEVEN(MONTH($B2))"
    background_color: str


# ─── Protocols ──────────────────────────────────────────────────────────

@runtime_checkable
class WorksheetHandle(Protocol):
    """Operations on a single worksheet (tab) within a spreadsheet."""

    @property
    def title(self) -> str: ...

    @property
    def hidden(self) -> bool: ...

    @property
    def row_count(self) -> int: ...

    @property
    def col_count(self) -> int: ...

    def get_values(self, range_a1: str) -> list[list[Any]]:
        """Return cell values for ``range_a1``; missing cells become ``""``."""

    def update_values(self, range_a1: str, values: list[list[Any]]) -> None:
        """Write ``values`` (rows x cols) into ``range_a1``.

        Values starting with ``"="`` are interpreted as formulas.
        """

    def append_rows(self, values: list[list[Any]]) -> None:
        """Append rows after the last non-empty row."""

    def clear(self) -> None:
        """Remove all values & formatting from the worksheet."""

    def format_range(self, range_a1: str, fmt: CellFormat) -> None:
        """Apply ``fmt`` over ``range_a1``. Empty ``fmt`` is a no-op."""

    def freeze(self, *, rows: int = 0, cols: int = 0) -> None:
        """Freeze the leading *rows*/*cols* so they stay visible on scroll."""

    def set_column_widths_px(
        self, *, start_col: str, widths: list[int]
    ) -> None:
        """Set column widths starting at column letter ``start_col``."""

    def set_hidden(self, hidden: bool) -> None:
        """Hide / unhide the tab from the worksheet bar."""

    def add_conditional_band(self, band: ConditionalBand) -> None:
        """Attach a conditional-format rule (e.g. alternating month band)."""

    def resize(self, *, rows: int | None = None, cols: int | None = None) -> None:
        """Resize the worksheet's row / column count."""


@runtime_checkable
class SheetsBackend(Protocol):
    """Spreadsheet-level operations. One backend instance ↔ one Sheet."""

    @property
    def spreadsheet_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    def list_worksheets(self) -> list[WorksheetHandle]: ...

    def has_worksheet(self, title: str) -> bool: ...

    def get_worksheet(self, title: str) -> WorksheetHandle: ...

    def create_worksheet(
        self, title: str, *, rows: int = 200, cols: int = 26
    ) -> WorksheetHandle: ...

    def delete_worksheet(self, title: str) -> None: ...

    def rename_worksheet(self, old_title: str, new_title: str) -> None: ...


# ─── A1 helpers (shared between backends and builders) ──────────────────

_A1_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
_A1_RANGE_RE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$")
# Open-ended range like "A2:M" — start row pinned, end row implicit.
# Maps to "row 2 to the end of the sheet" in Google Sheets semantics.
_A1_OPEN_RANGE_RE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)$")


def col_letter_to_index(letter: str) -> int:
    """``'A'`` -> 0, ``'B'`` -> 1, ``'AA'`` -> 26, …"""
    n = 0
    for c in letter.upper():
        if not ("A" <= c <= "Z"):
            raise ValueError(f"invalid column letter: {letter!r}")
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def col_index_to_letter(index: int) -> str:
    """0 -> ``'A'``, 25 -> ``'Z'``, 26 -> ``'AA'``, …"""
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    n = index + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def parse_a1_cell(addr: str) -> tuple[int, int]:
    """``'B3'`` -> (row=2, col=1) — both 0-based."""
    m = _A1_CELL_RE.match(addr.strip())
    if not m:
        raise ValueError(f"invalid A1 cell address: {addr!r}")
    col = col_letter_to_index(m.group(1))
    row = int(m.group(2)) - 1
    return row, col


def parse_a1_range(range_a1: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ``((r1, c1), (r2, c2))`` inclusive, 0-based.

    Accepted shapes:

    * Single cell: ``"A1"`` → ``((0, 0), (0, 0))``.
    * Closed range: ``"A1:B5"`` → ``((0, 0), (4, 1))``.
    * Open-ended range: ``"A2:M"`` (no end row) → ``((1, 0), (-1, 12))``.
      Callers that care about the end row check for ``r2 == -1`` and
      substitute the worksheet's current ``row_count`` (this is what
      Google Sheets does when you write ``A2:M`` in the UI).

    Raises:
        ValueError: anything that doesn't match one of the three shapes.
    """
    s = range_a1.strip()
    if "!" in s:
        s = s.split("!", 1)[1]
    m = _A1_RANGE_RE.match(s)
    if m:
        c1 = col_letter_to_index(m.group(1))
        r1 = int(m.group(2)) - 1
        c2 = col_letter_to_index(m.group(3))
        r2 = int(m.group(4)) - 1
        return (r1, c1), (r2, c2)
    m_open = _A1_OPEN_RANGE_RE.match(s)
    if m_open:
        c1 = col_letter_to_index(m_open.group(1))
        r1 = int(m_open.group(2)) - 1
        c2 = col_letter_to_index(m_open.group(3))
        return (r1, c1), (-1, c2)
    # Try single cell.
    rc = parse_a1_cell(s)
    return rc, rc


# ─── In-memory fake (test backend) ──────────────────────────────────────

@dataclass
class _FakeWorksheet:
    """In-memory worksheet — mutable, cheap to introspect from tests."""

    title: str
    row_count: int = 200
    col_count: int = 26
    hidden: bool = False
    # (row, col) -> value (or formula starting with "=").
    _cells: dict[tuple[int, int], Any] = field(default_factory=dict)
    # Records of formatting + freeze + width calls, preserved in order
    # so tests can assert "we asked for X formatting".
    _format_log: list[tuple[str, CellFormat]] = field(default_factory=list)
    _freeze_rows: int = 0
    _freeze_cols: int = 0
    _column_widths: dict[str, int] = field(default_factory=dict)
    _conditional_bands: list[ConditionalBand] = field(default_factory=list)

    # ─── WorksheetHandle protocol ───
    def get_values(self, range_a1: str) -> list[list[Any]]:
        (r1, c1), (r2, c2) = parse_a1_range(range_a1)
        out: list[list[Any]] = []
        for r in range(r1, r2 + 1):
            row: list[Any] = []
            for c in range(c1, c2 + 1):
                row.append(self._cells.get((r, c), ""))
            out.append(row)
        return out

    def update_values(self, range_a1: str, values: list[list[Any]]) -> None:
        (r1, c1), (r2, c2) = parse_a1_range(range_a1)
        height = r2 - r1 + 1
        width = c2 - c1 + 1
        if len(values) > height or any(len(row) > width for row in values):
            raise ValueError(
                f"values do not fit in {range_a1}: got {len(values)}x"
                f"{max((len(r) for r in values), default=0)}, range is {height}x{width}"
            )
        for dr, row in enumerate(values):
            for dc, val in enumerate(row):
                self._cells[(r1 + dr, c1 + dc)] = val

    def append_rows(self, values: list[list[Any]]) -> None:
        # Find first empty row (any row with no cells in any column).
        used_rows = {r for (r, _c) in self._cells.keys()}
        next_row = (max(used_rows) + 1) if used_rows else 0
        for offset, row in enumerate(values):
            for c, val in enumerate(row):
                self._cells[(next_row + offset, c)] = val
        # Grow row_count if needed.
        if used_rows and (next_row + len(values)) > self.row_count:
            self.row_count = next_row + len(values)

    def clear(self) -> None:
        self._cells.clear()
        self._format_log.clear()
        self._freeze_rows = 0
        self._freeze_cols = 0
        self._column_widths.clear()
        self._conditional_bands.clear()

    def format_range(self, range_a1: str, fmt: CellFormat) -> None:
        if fmt.is_empty():
            return
        self._format_log.append((range_a1, fmt))

    def freeze(self, *, rows: int = 0, cols: int = 0) -> None:
        self._freeze_rows = rows
        self._freeze_cols = cols

    def set_column_widths_px(self, *, start_col: str, widths: list[int]) -> None:
        idx = col_letter_to_index(start_col)
        for offset, w in enumerate(widths):
            self._column_widths[col_index_to_letter(idx + offset)] = w

    def set_hidden(self, hidden: bool) -> None:
        self.hidden = hidden

    def add_conditional_band(self, band: ConditionalBand) -> None:
        self._conditional_bands.append(band)

    def resize(self, *, rows: int | None = None, cols: int | None = None) -> None:
        if rows is not None:
            self.row_count = rows
        if cols is not None:
            self.col_count = cols

    # ─── Test introspection helpers ───
    def cell(self, addr: str) -> Any:
        """Return the value stored at ``addr`` (e.g. ``"B3"``)."""
        r, c = parse_a1_cell(addr)
        return self._cells.get((r, c), "")

    def format_calls(self) -> list[tuple[str, CellFormat]]:
        """Returns a *copy* of the formatting log — read-only for tests."""
        return list(self._format_log)

    @property
    def freeze_state(self) -> tuple[int, int]:
        return (self._freeze_rows, self._freeze_cols)

    @property
    def column_widths(self) -> dict[str, int]:
        return dict(self._column_widths)

    @property
    def conditional_bands(self) -> list[ConditionalBand]:
        return list(self._conditional_bands)


@dataclass
class FakeSheetsBackend:
    """In-memory :class:`SheetsBackend`. Lookup-by-title only."""

    spreadsheet_id: str = "fake_sheet"
    title: str = "Fake Spreadsheet"
    _worksheets: list[_FakeWorksheet] = field(default_factory=list)

    # ─── SheetsBackend protocol ───
    def list_worksheets(self) -> list[WorksheetHandle]:
        return list(self._worksheets)

    def has_worksheet(self, title: str) -> bool:
        return any(ws.title == title for ws in self._worksheets)

    def get_worksheet(self, title: str) -> WorksheetHandle:
        for ws in self._worksheets:
            if ws.title == title:
                return ws
        from .exceptions import SheetsNotFoundError

        raise SheetsNotFoundError(f"worksheet {title!r} not found in {self.title!r}")

    def create_worksheet(
        self, title: str, *, rows: int = 200, cols: int = 26
    ) -> WorksheetHandle:
        from .exceptions import SheetsAlreadyExistsError

        if self.has_worksheet(title):
            raise SheetsAlreadyExistsError(f"worksheet {title!r} already exists")
        ws = _FakeWorksheet(title=title, row_count=rows, col_count=cols)
        self._worksheets.append(ws)
        return ws

    def delete_worksheet(self, title: str) -> None:
        before = len(self._worksheets)
        self._worksheets = [ws for ws in self._worksheets if ws.title != title]
        if len(self._worksheets) == before:
            from .exceptions import SheetsNotFoundError

            raise SheetsNotFoundError(f"worksheet {title!r} not found")

    def rename_worksheet(self, old_title: str, new_title: str) -> None:
        if old_title == new_title:
            return
        if self.has_worksheet(new_title):
            from .exceptions import SheetsAlreadyExistsError

            raise SheetsAlreadyExistsError(
                f"cannot rename {old_title!r} to {new_title!r}: target already exists"
            )
        ws = self.get_worksheet(old_title)
        # _FakeWorksheet exposes a settable title; cast for static checkers.
        assert isinstance(ws, _FakeWorksheet)
        ws.title = new_title
