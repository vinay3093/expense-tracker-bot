"""Concrete :class:`SheetsBackend` powered by gspread.

Lazy imports the ``gspread`` and ``google-auth`` packages so the rest
of the test suite (which uses :class:`FakeSheetsBackend`) doesn't have
to install them.

Error translation policy
------------------------
gspread raises a handful of exceptions that we never let propagate
unwrapped — callers should ONLY have to catch our typed
:class:`SheetsError` hierarchy. The helpers below translate:

* :class:`gspread.exceptions.WorksheetNotFound`
  → :class:`SheetsNotFoundError`
* :class:`gspread.exceptions.APIError` (auth / not-found / rate-limit)
  → :class:`SheetsAuthError` / :class:`SheetsNotFoundError` /
    :class:`SheetsAPIError`
* :class:`google.auth.exceptions.DefaultCredentialsError`
  → :class:`SheetsAuthError`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend import (
    CellFormat,
    ConditionalBand,
    WorksheetHandle,
    col_letter_to_index,
    parse_a1_range,
)
from .exceptions import (
    SheetsAlreadyExistsError,
    SheetsAPIError,
    SheetsAuthError,
    SheetsConfigError,
    SheetsNotFoundError,
)

if TYPE_CHECKING:  # pragma: no cover
    from gspread.spreadsheet import Spreadsheet
    from gspread.worksheet import Worksheet


# ─── Authentication / construction ─────────────────────────────────────

def _load_gspread() -> Any:
    """Import gspread lazily; map import errors to a typed exception."""
    try:
        import gspread

        return gspread
    except ImportError as exc:  # pragma: no cover
        raise SheetsConfigError(
            "gspread is not installed. Install with: "
            "pip install -e '.[all]' or pip install gspread google-auth"
        ) from exc


def open_spreadsheet(
    *,
    service_account_path: str | Path,
    spreadsheet_id: str,
    timeout_s: float = 30.0,
) -> GspreadSheetsBackend:
    """Authorise the service-account JSON and open ``spreadsheet_id``.

    Returns a :class:`GspreadSheetsBackend` ready to use. Network +
    auth errors are mapped to the typed :class:`SheetsError` hierarchy.
    """
    if not service_account_path:
        raise SheetsConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is empty — set it in .env to the "
            "path of your service-account JSON file."
        )
    sa_path = Path(service_account_path)
    if not sa_path.is_file():
        raise SheetsConfigError(
            f"service-account JSON not found at {sa_path}. Download it from "
            f"the Google Cloud Console and put the path in GOOGLE_SERVICE_ACCOUNT_JSON."
        )
    if not spreadsheet_id:
        raise SheetsConfigError(
            "EXPENSE_SHEET_ID is empty — set it in .env to the long token "
            "between /spreadsheets/d/ and /edit in your Google Sheet URL."
        )

    gspread_pkg = _load_gspread()

    try:
        client = gspread_pkg.service_account(filename=str(sa_path))
    except Exception as exc:
        raise SheetsAuthError(
            f"failed to authorise with service account at {sa_path}: {exc}"
        ) from exc

    try:
        spreadsheet = client.open_by_key(spreadsheet_id)
    except gspread_pkg.exceptions.APIError as exc:
        raise _translate_api_error(exc, default=SheetsAPIError) from exc
    except Exception as exc:
        raise SheetsAPIError(f"failed to open spreadsheet {spreadsheet_id!r}: {exc}") from exc

    return GspreadSheetsBackend(
        client=client,
        spreadsheet=spreadsheet,
        timeout_s=timeout_s,
    )


def _translate_api_error(exc: Any, *, default: type[Exception]) -> Exception:
    """Best-effort translate a gspread APIError to our typed hierarchy."""
    msg = str(exc)
    lowered = msg.lower()
    if "permission" in lowered or "unauthor" in lowered or "forbidden" in lowered:
        return SheetsAuthError(
            f"service account is not authorised for this spreadsheet — share "
            f"the sheet with the service-account email. ({msg})"
        )
    if "not found" in lowered or "notfound" in lowered:
        return SheetsNotFoundError(msg)
    if isinstance(default, type) and issubclass(default, Exception):
        return default(msg)
    return SheetsAPIError(msg)


# ─── Worksheet wrapper ──────────────────────────────────────────────────

@dataclass
class GspreadWorksheet:
    """Adapter from :class:`WorksheetHandle` to a gspread ``Worksheet``."""

    _ws: Worksheet
    _spreadsheet: Spreadsheet
    _gspread: Any  # gspread module — used for exception types

    @property
    def title(self) -> str:
        return self._ws.title

    @property
    def hidden(self) -> bool:
        # gspread caches worksheet metadata; ``hidden`` reflects the
        # last fetch. Trustworthy enough for our use case.
        return bool(getattr(self._ws, "_properties", {}).get("hidden", False))

    @property
    def row_count(self) -> int:
        return self._ws.row_count

    @property
    def col_count(self) -> int:
        return self._ws.col_count

    @property
    def gid(self) -> int:
        return self._ws.id

    # ─── Read / write ───────────────────────────────────────────────────
    def get_values(self, range_a1: str) -> list[list[Any]]:
        try:
            return self._ws.get(range_a1)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def update_values(self, range_a1: str, values: list[list[Any]]) -> None:
        try:
            self._ws.update(
                range_name=range_a1,
                values=values,
                value_input_option="USER_ENTERED",
            )
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def append_rows(self, values: list[list[Any]]) -> None:
        if not values:
            return
        try:
            self._ws.append_rows(values, value_input_option="USER_ENTERED")
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def clear(self) -> None:
        try:
            self._ws.clear()
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    # ─── Formatting ─────────────────────────────────────────────────────
    def format_range(self, range_a1: str, fmt: CellFormat) -> None:
        if fmt.is_empty():
            return
        gs_fmt = _cell_format_to_gspread(fmt)
        if not gs_fmt:
            return
        try:
            self._ws.format(range_a1, gs_fmt)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def freeze(self, *, rows: int = 0, cols: int = 0) -> None:
        try:
            self._ws.freeze(rows=rows, cols=cols)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def set_column_widths_px(self, *, start_col: str, widths: list[int]) -> None:
        if not widths:
            return
        start_index = col_letter_to_index(start_col)
        requests = []
        for offset, w in enumerate(widths):
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self.gid,
                        "dimension": "COLUMNS",
                        "startIndex": start_index + offset,
                        "endIndex": start_index + offset + 1,
                    },
                    "properties": {"pixelSize": int(w)},
                    "fields": "pixelSize",
                }
            })
        try:
            self._spreadsheet.batch_update({"requests": requests})
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def set_hidden(self, hidden: bool) -> None:
        try:
            if hidden:
                self._ws.hide()
            else:
                self._ws.show()
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc
        # Patch the cached property dict so a subsequent ``hidden``
        # read reflects what we just changed.
        props = getattr(self._ws, "_properties", None)
        if isinstance(props, dict):
            props["hidden"] = hidden

    def add_conditional_band(self, band: ConditionalBand) -> None:
        (r1, c1), (r2, c2) = parse_a1_range(band.range_a1)
        # Treat an open-ended range like "A2:Z" (no end row) as "to end
        # of sheet". parse_a1_range returns -1 there because we'd parse
        # "A2:Z" as missing — in practice we always provide an explicit
        # end row, so this guard is defensive.
        end_row = r2 + 1 if r2 >= 0 else self.row_count
        request = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": self.gid,
                        "startRowIndex": r1,
                        "endRowIndex": end_row,
                        "startColumnIndex": c1,
                        "endColumnIndex": c2 + 1,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": band.predicate_formula}],
                        },
                        "format": {
                            "backgroundColor": _hex_to_rgb_dict(band.background_color),
                        },
                    },
                },
                "index": 0,
            }
        }
        try:
            self._spreadsheet.batch_update({"requests": [request]})
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def resize(self, *, rows: int | None = None, cols: int | None = None) -> None:
        try:
            self._ws.resize(rows=rows, cols=cols)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc


# ─── Spreadsheet wrapper ────────────────────────────────────────────────

@dataclass
class GspreadSheetsBackend:
    """Adapter from :class:`SheetsBackend` to a gspread ``Spreadsheet``."""

    client: Any
    spreadsheet: Spreadsheet
    timeout_s: float = 30.0
    _gspread: Any = None

    def __post_init__(self) -> None:
        self._gspread = _load_gspread()

    @property
    def spreadsheet_id(self) -> str:
        return self.spreadsheet.id

    @property
    def title(self) -> str:
        return self.spreadsheet.title

    @property
    def url(self) -> str:
        return self.spreadsheet.url

    @property
    def service_account_email(self) -> str:
        """Email of the service account currently authorised — useful for
        sharing the sheet with the right principal.

        gspread 5.x exposed the credentials as ``client.auth``; 6.x moved
        them inside ``client.http_client.auth``. We walk both paths so
        the helper survives a future relocation too.
        """
        candidates = [
            getattr(self.client, "auth", None),
            getattr(getattr(self.client, "http_client", None), "auth", None),
            getattr(getattr(self.client, "session", None), "credentials", None),
        ]
        for c in candidates:
            email = getattr(c, "service_account_email", None)
            if email:
                return email
        return "(unknown)"

    def list_worksheets(self) -> list[WorksheetHandle]:
        try:
            wss = self.spreadsheet.worksheets()
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc
        return [
            GspreadWorksheet(_ws=w, _spreadsheet=self.spreadsheet, _gspread=self._gspread)
            for w in wss
        ]

    def has_worksheet(self, title: str) -> bool:
        try:
            self.spreadsheet.worksheet(title)
            return True
        except self._gspread.exceptions.WorksheetNotFound:
            return False
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def get_worksheet(self, title: str) -> WorksheetHandle:
        try:
            ws = self.spreadsheet.worksheet(title)
        except self._gspread.exceptions.WorksheetNotFound as exc:
            raise SheetsNotFoundError(f"worksheet {title!r} not found") from exc
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc
        return GspreadWorksheet(
            _ws=ws, _spreadsheet=self.spreadsheet, _gspread=self._gspread
        )

    def create_worksheet(
        self, title: str, *, rows: int = 200, cols: int = 26
    ) -> WorksheetHandle:
        if self.has_worksheet(title):
            raise SheetsAlreadyExistsError(f"worksheet {title!r} already exists")
        try:
            ws = self.spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc
        return GspreadWorksheet(
            _ws=ws, _spreadsheet=self.spreadsheet, _gspread=self._gspread
        )

    def delete_worksheet(self, title: str) -> None:
        try:
            ws = self.spreadsheet.worksheet(title)
        except self._gspread.exceptions.WorksheetNotFound as exc:
            raise SheetsNotFoundError(f"worksheet {title!r} not found") from exc
        try:
            self.spreadsheet.del_worksheet(ws)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc

    def rename_worksheet(self, old_title: str, new_title: str) -> None:
        if old_title == new_title:
            return
        if self.has_worksheet(new_title):
            raise SheetsAlreadyExistsError(
                f"cannot rename {old_title!r} to {new_title!r}: target already exists"
            )
        try:
            ws = self.spreadsheet.worksheet(old_title)
        except self._gspread.exceptions.WorksheetNotFound as exc:
            raise SheetsNotFoundError(f"worksheet {old_title!r} not found") from exc
        try:
            ws.update_title(new_title)
        except self._gspread.exceptions.APIError as exc:
            raise _translate_api_error(exc, default=SheetsAPIError) from exc


# ─── Format conversion helpers ──────────────────────────────────────────

_HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _hex_to_rgb_dict(hex_color: str) -> dict[str, float]:
    """``"#1F1F1F"`` -> ``{"red": 0.121, "green": 0.121, "blue": 0.121}``."""
    m = _HEX_COLOR_RE.match(hex_color)
    if not m:
        raise SheetsConfigError(f"invalid hex color: {hex_color!r}")
    h = m.group(1)
    return {
        "red": int(h[0:2], 16) / 255.0,
        "green": int(h[2:4], 16) / 255.0,
        "blue": int(h[4:6], 16) / 255.0,
    }


def _cell_format_to_gspread(fmt: CellFormat) -> dict[str, Any]:
    """Translate :class:`CellFormat` to gspread's format-dict shape."""
    out: dict[str, Any] = {}

    if fmt.background_color:
        out["backgroundColor"] = _hex_to_rgb_dict(fmt.background_color)

    text_format: dict[str, Any] = {}
    if fmt.foreground_color:
        text_format["foregroundColor"] = _hex_to_rgb_dict(fmt.foreground_color)
    if fmt.bold is not None:
        text_format["bold"] = fmt.bold
    if fmt.italic is not None:
        text_format["italic"] = fmt.italic
    if fmt.font_size is not None:
        text_format["fontSize"] = fmt.font_size
    if text_format:
        out["textFormat"] = text_format

    if fmt.horizontal_alignment:
        out["horizontalAlignment"] = fmt.horizontal_alignment
    if fmt.vertical_alignment:
        out["verticalAlignment"] = fmt.vertical_alignment

    if fmt.number_format:
        out["numberFormat"] = {"type": "NUMBER", "pattern": fmt.number_format}

    if fmt.wrap is not None:
        out["wrapStrategy"] = "WRAP" if fmt.wrap else "CLIP"

    return out
