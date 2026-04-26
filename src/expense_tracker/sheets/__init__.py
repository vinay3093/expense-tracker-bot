"""Google Sheets layer — public API.

Three things you ever need from outside this package:

1. :func:`get_sheets_backend` — build a :class:`SheetsBackend` from
   settings (real gspread, or a fake for tests / offline mode).
2. :func:`get_sheet_format` — load the YAML formatting prefs.
3. The builders / helpers — :func:`build_month_tab`,
   :func:`build_ytd_tab`, :func:`setup_year`, :func:`init_transactions_tab`,
   :func:`append_transactions`.
4. :class:`CurrencyConverter` — convert chat-logged amounts to USD
   before writing them.

Everything else (column letters, formula builders, layout constants)
is exported for tests / advanced use, but most callers won't need it.
"""

from __future__ import annotations

from .backend import (
    CellFormat,
    ConditionalBand,
    FakeSheetsBackend,
    SheetsBackend,
    WorksheetHandle,
    col_index_to_letter,
    col_letter_to_index,
)
from .currency import (
    ConversionResult,
    CurrencyConverter,
    CurrencyError,
    get_converter,
    quick_convert_to_primary,
)
from .exceptions import (
    SheetFormatError,
    SheetsAlreadyExistsError,
    SheetsAPIError,
    SheetsAuthError,
    SheetsConfigError,
    SheetsError,
    SheetsNotFoundError,
)
from .factory import get_sheets_backend
from .format import (
    MonthlyFormat,
    SheetFormat,
    TransactionsFormat,
    YTDFormat,
    get_sheet_format,
    reset_format_cache_for_tests,
)
from .month_builder import (
    MonthLayout,
    build_month_tab,
    daily_cell_formula,
    daily_total_cell_formula,
)
from .transactions import (
    TRANSACTIONS_COLUMNS,
    ColumnType,
    TransactionColumn,
    TransactionRow,
    append_transactions,
    init_transactions_tab,
)
from .transactions import (
    col_for as transactions_col_for,
)
from .transactions import (
    header_row as transactions_header_row,
)
from .transactions import (
    index_for as transactions_index_for,
)
from .year_builder import (
    YearSetupReport,
    discover_years_present,
    ensure_month_tab,
    ensure_ytd_tab,
    hide_previous_year_monthly_tabs,
    setup_year,
)
from .ytd_builder import (
    YTDLayout,
    build_ytd_tab,
)

__all__ = [
    "TRANSACTIONS_COLUMNS",
    "CellFormat",
    "ColumnType",
    "ConditionalBand",
    "ConversionResult",
    "CurrencyConverter",
    "CurrencyError",
    "FakeSheetsBackend",
    "MonthLayout",
    "MonthlyFormat",
    "SheetFormat",
    "SheetFormatError",
    "SheetsAPIError",
    "SheetsAlreadyExistsError",
    "SheetsAuthError",
    "SheetsBackend",
    "SheetsConfigError",
    "SheetsError",
    "SheetsNotFoundError",
    "TransactionColumn",
    "TransactionRow",
    "TransactionsFormat",
    "WorksheetHandle",
    "YTDFormat",
    "YTDLayout",
    "YearSetupReport",
    "append_transactions",
    "build_month_tab",
    "build_ytd_tab",
    "col_index_to_letter",
    "col_letter_to_index",
    "daily_cell_formula",
    "daily_total_cell_formula",
    "discover_years_present",
    "ensure_month_tab",
    "ensure_ytd_tab",
    "get_converter",
    "get_sheet_format",
    "get_sheets_backend",
    "hide_previous_year_monthly_tabs",
    "init_transactions_tab",
    "quick_convert_to_primary",
    "reset_format_cache_for_tests",
    "setup_year",
    "transactions_col_for",
    "transactions_header_row",
    "transactions_index_for",
]
