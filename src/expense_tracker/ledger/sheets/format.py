"""Parse and validate ``sheet_format.yaml``.

The file holds *visual* preferences only — the structural contract
(columns of Transactions, regions of a monthly tab, formula bodies)
lives in code, where it can be unit-tested. Everything in YAML is a
knob a human might want to adjust without re-deploying.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...config import get_settings
from .exceptions import SheetFormatError

_DEFAULT_FORMAT_FILE = Path(__file__).parent / "data" / "sheet_format.yaml"


# ─── Emphasis (visual "loud the >0 cells" rules) ───────────────────────
#
# Both monthly and YTD grids share a single emphasis vocabulary:
#
#   * data_cell_base       - quiet style for daily-grid category cells
#                            (mostly zeros; we want them to vanish into
#                            the background).
#   * data_cell_emphasis   - applied via conditional band when the cell
#                            value > 0. Bigger + bolder + darker so real
#                            spending visually pops.
#   * total_cell_base      - same idea, but for the per-day TOTAL column.
#   * total_cell_emphasis  - "real day" emphasis: the day's spend stands
#                            out even more than category cells.
#   * category_total       - always-on style for the per-column totals
#                            (bottom row of the grid).
#   * grand_total          - always-on style for the corner cell — the
#                            month's grand total. The eye-magnet.

class CellStyle(BaseModel):
    """Lightweight, YAML-friendly description of a cell's text styling.

    Maps cleanly onto :class:`~expense_tracker.ledger.sheets.backend.CellFormat`
    while staying small enough to be readable in YAML. Used for the
    emphasis rules that make non-zero cells visually pop.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bold: bool = False
    font_size: int = Field(default=10, ge=6, le=36)
    foreground: str = "#000000"
    background: str | None = None


class EmphasisFormatting(BaseModel):
    """Visual emphasis rules shared by monthly + YTD tabs.

    Every cell in a monthly daily-grid is a SUMIFS that defaults to 0.
    Without emphasis the grid is a sea of "0.00" — readable but flat.
    These rules set a quiet baseline + a louder conditional emphasis so
    actual spending reads at a glance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_cell_base: CellStyle = Field(
        default_factory=lambda: CellStyle(font_size=10, foreground="#9AA0A6"),
    )
    data_cell_emphasis: CellStyle = Field(
        default_factory=lambda: CellStyle(
            font_size=11, foreground="#1F1F1F", bold=True
        ),
    )
    total_cell_base: CellStyle = Field(
        default_factory=lambda: CellStyle(font_size=10, foreground="#9AA0A6"),
    )
    total_cell_emphasis: CellStyle = Field(
        default_factory=lambda: CellStyle(
            font_size=12, foreground="#0B5394", bold=True
        ),
    )
    category_total: CellStyle = Field(
        default_factory=lambda: CellStyle(
            font_size=11, foreground="#0B5394", bold=True, background="#F0F0F0",
        ),
    )
    grand_total: CellStyle = Field(
        default_factory=lambda: CellStyle(
            font_size=13, foreground="#073763", bold=True, background="#E8F0FE",
        ),
    )


# ─── Reusable formatting blocks ─────────────────────────────────────────

class TransactionsFormatting(BaseModel):
    """Visual rules for the Transactions tab."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    freeze_rows: int = Field(default=1, ge=0)
    header_bold: bool = True
    header_background: str = "#1F1F1F"
    header_foreground: str = "#FFFFFF"
    month_band_color: str | None = "#F2F2F2"
    number_format: str = "#,##0.00"
    column_widths: dict[str, int] = Field(default_factory=dict)


class MonthlyFormatting(BaseModel):
    """Visual rules for monthly tabs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title_font_size: int = Field(default=14, ge=8, le=36)
    title_bold: bool = True
    summary_label_bold: bool = True
    summary_value_bold: bool = True
    grid_header_background: str = "#E8E8E8"
    grid_total_row_background: str = "#F0F0F0"
    grid_total_column_background: str = "#F8F8F8"
    breakdown_title_bold: bool = True
    number_format: str = "#,##0.00"
    freeze_rows: int = Field(default=10, ge=0)
    freeze_cols: int = Field(default=2, ge=0)
    date_column_width: int = Field(default=60, ge=20)
    day_column_width: int = Field(default=55, ge=20)
    category_column_width: int = Field(default=95, ge=40)
    total_column_width: int = Field(default=105, ge=40)


class YTDFormatting(BaseModel):
    """Visual rules for the YTD tab."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title_font_size: int = Field(default=14, ge=8, le=36)
    title_bold: bool = True
    summary_label_bold: bool = True
    summary_value_bold: bool = True
    grid_header_background: str = "#E8E8E8"
    grid_total_row_background: str = "#F0F0F0"
    grid_total_column_background: str = "#F8F8F8"
    breakdown_title_bold: bool = True
    number_format: str = "#,##0.00"
    freeze_rows: int = Field(default=10, ge=0)
    freeze_cols: int = Field(default=1, ge=0)
    month_column_width: int = Field(default=110, ge=40)
    category_column_width: int = Field(default=95, ge=40)
    total_column_width: int = Field(default=110, ge=40)


# ─── Per-tab format blocks ──────────────────────────────────────────────

class TransactionsFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sheet_name: str = Field(default="Transactions", min_length=1)
    formatting: TransactionsFormatting = Field(default_factory=TransactionsFormatting)


class MonthlyFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sheet_name_pattern: str = Field(default="{month_name} {year}", min_length=1)
    title_pattern: str = Field(default="{month_name} {year} Expenses")
    formatting: MonthlyFormatting = Field(default_factory=MonthlyFormatting)
    breakdown_top_n_per_category: int = Field(default=10, ge=1, le=100)

    @field_validator("sheet_name_pattern")
    @classmethod
    def _has_required_tokens(cls, v: str) -> str:
        if "{month_name}" not in v and "{month_short}" not in v and "{month_num}" not in v:
            raise ValueError(
                "monthly.sheet_name_pattern must include {month_name}, "
                "{month_short}, or {month_num}"
            )
        if "{year}" not in v:
            raise ValueError("monthly.sheet_name_pattern must include {year}")
        return v


class YTDFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sheet_name_pattern: str = Field(default="YTD {year}", min_length=1)
    title_pattern: str = Field(default="Year to Date — {year}")
    formatting: YTDFormatting = Field(default_factory=YTDFormatting)
    top_vendors_count: int = Field(default=10, ge=1, le=100)

    @field_validator("sheet_name_pattern")
    @classmethod
    def _has_year_token(cls, v: str) -> str:
        if "{year}" not in v:
            raise ValueError("ytd.sheet_name_pattern must include {year}")
        return v


# ─── Top-level model ────────────────────────────────────────────────────

class SheetFormat(BaseModel):
    """Strongly-typed wrapper around ``sheet_format.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 2

    primary_currency: str = Field(default="USD", min_length=3, max_length=3)
    secondary_currency: str | None = Field(default="INR")

    transactions: TransactionsFormat = Field(default_factory=TransactionsFormat)
    monthly: MonthlyFormat = Field(default_factory=MonthlyFormat)
    ytd: YTDFormat = Field(default_factory=YTDFormat)
    emphasis: EmphasisFormatting = Field(default_factory=EmphasisFormatting)

    @field_validator("primary_currency")
    @classmethod
    def _upper_primary(cls, v: str) -> str:
        return v.upper()

    @field_validator("secondary_currency")
    @classmethod
    def _upper_secondary(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) != 3:
            raise ValueError("secondary_currency must be a 3-letter ISO code or null")
        return v.upper()

    # ─── Pattern formatters ─────────────────────────────────────────────
    def monthly_sheet_name(
        self, *, month_name: str, month_short: str, month_num: int, year: int
    ) -> str:
        return self.monthly.sheet_name_pattern.format(
            month_name=month_name,
            month_short=month_short,
            month_num=f"{month_num:02d}",
            year=year,
        )

    def monthly_title(
        self, *, month_name: str, month_short: str, month_num: int, year: int
    ) -> str:
        return self.monthly.title_pattern.format(
            month_name=month_name,
            month_short=month_short,
            month_num=f"{month_num:02d}",
            year=year,
        )

    def ytd_sheet_name(self, *, year: int) -> str:
        return self.ytd.sheet_name_pattern.format(year=year)

    def ytd_title(self, *, year: int) -> str:
        return self.ytd.title_pattern.format(year=year)

    # ─── Construction ───────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SheetFormat:
        try:
            return cls(**data)
        except Exception as exc:
            raise SheetFormatError(f"sheet_format.yaml is malformed: {exc}") from exc

    @classmethod
    def from_yaml(cls, path: str | Path) -> SheetFormat:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError as exc:
            raise SheetFormatError(f"sheet_format.yaml not found at {path!r}") from exc
        except yaml.YAMLError as exc:
            raise SheetFormatError(f"sheet_format.yaml is invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise SheetFormatError("sheet_format.yaml must be a YAML mapping at the top level")
        return cls.from_dict(data)


# ─── Module-level loader (cached) ───────────────────────────────────────

@lru_cache(maxsize=1)
def _load_default_format() -> SheetFormat:
    return SheetFormat.from_yaml(_DEFAULT_FORMAT_FILE)


@lru_cache(maxsize=8)
def _load_format_from_path(path: str) -> SheetFormat:
    return SheetFormat.from_yaml(path)


def get_sheet_format() -> SheetFormat:
    """Return the sheet format configured for this process.

    Uses the YAML at ``Settings.SHEET_FORMAT_FILE`` when set, else the
    bundled default. Cached per-path; call
    :func:`reset_format_cache_for_tests` from tests that need a fresh
    file picked up.
    """
    cfg = get_settings()
    override = cfg.SHEET_FORMAT_FILE
    if not override:
        return _load_default_format()
    return _load_format_from_path(override)


def reset_format_cache_for_tests() -> None:
    """Drop all cached :class:`SheetFormat` instances. Tests only."""
    _load_default_format.cache_clear()
    _load_format_from_path.cache_clear()
