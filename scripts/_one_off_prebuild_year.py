"""One-off: pre-create monthly tabs for the next 12 months + YTD 2027.

Run from the repo root with the local .venv activated.  Designed to be safe
to re-run — every tab that already exists is skipped (no clobbering).

Why this exists
---------------
The bot's "create new monthly tab on first message of the month" path burns
~80-100 Sheets API calls in one burst, which is over Google's free 60/min
quota and causes the user's first message-of-the-month to fail with 429.
Pre-creating all upcoming months from the laptop (where there's no
per-process Sheets rate-limit) sidesteps the issue for a full year.

This file lives in scripts/ so the repo's pyproject `[tool.ruff]` excludes
catch it; it is not part of the deployed package.
"""

from __future__ import annotations

import calendar
import os
import sys
import time
from datetime import date

from dotenv import load_dotenv

# Make src/ importable when run as `python scripts/_one_off_prebuild_year.py`
sys.path.insert(0, "src")

from expense_tracker.config import get_settings
from expense_tracker.ledger.sheets import (
    SheetsAlreadyExistsError,
    SheetsError,
    build_month_tab,
    build_ytd_tab,
    get_sheet_format,
)
from expense_tracker.ledger.sheets.factory import get_sheets_backend
from expense_tracker.extractor import get_registry

load_dotenv(".env")

# ─── Plan: build everything from NEXT month through end of year + 1 ────
# Today is Jun 1, 2026 → next 12 months = Jul 2026 .. Jun 2027.
# Plus YTD 2027 because that'll be needed when 2027 starts.
PLAN: list[tuple[int, int]] = [
    (2026, 7), (2026, 8), (2026, 9), (2026, 10), (2026, 11), (2026, 12),
    (2027, 1), (2027, 2), (2027, 3), (2027, 4),  (2027, 5),  (2027, 6),
]
YTD_YEARS_TO_BUILD = [2027]

# Sleep between operations to stay safely under 60 writes/min Sheets quota.
# Each build does ~80 calls; the API resets the quota window every 60s.
SLEEP_BETWEEN_S = 60

# ─── Setup backend ─────────────────────────────────────────────────────
cfg = get_settings()
backend = get_sheets_backend(cfg, fake=False)
fmt = get_sheet_format()
categories = get_registry().canonical_names()

print(f"Categories     : {len(categories)} ({', '.join(categories[:4])}...)")
print(f"Months to build: {len(PLAN)}")
print(f"YTD to build   : {YTD_YEARS_TO_BUILD}")
print(f"Pacing         : {SLEEP_BETWEEN_S}s sleep between ops")
print(f"Estimated time : ~{(len(PLAN) + len(YTD_YEARS_TO_BUILD)) * (SLEEP_BETWEEN_S + 5) // 60} min")
print()

# ─── Execute ───────────────────────────────────────────────────────────
created: list[str] = []
skipped: list[str] = []
failed: list[tuple[str, str]] = []
total = len(PLAN) + len(YTD_YEARS_TO_BUILD)
start = time.time()

def _log(i: int, label: str, status: str) -> None:
    elapsed = int(time.time() - start)
    print(f"[{i:2d}/{total}] +{elapsed:4d}s  {label:18s}  {status}", flush=True)

for i, (year, month) in enumerate(PLAN, start=1):
    name = fmt.monthly_sheet_name(
        month_name=calendar.month_name[month],
        month_short=calendar.month_abbr[month],
        month_num=month,
        year=year,
    )
    try:
        build_month_tab(
            backend, fmt,
            year=year, month=month,
            categories=categories,
            overwrite=False,
        )
        created.append(name)
        _log(i, name, "✓ created")
    except SheetsAlreadyExistsError:
        skipped.append(name)
        _log(i, name, "· already exists (skipped)")
    except SheetsError as exc:
        failed.append((name, f"{type(exc).__name__}: {exc}"))
        _log(i, name, f"✗ FAILED — {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        failed.append((name, f"{type(exc).__name__}: {exc}"))
        _log(i, name, f"✗ FAILED — {type(exc).__name__}")
    # Pace — don't sleep after the LAST month before YTD, just go straight there.
    if i < len(PLAN):
        time.sleep(SLEEP_BETWEEN_S)

# YTD tabs
for j, year in enumerate(YTD_YEARS_TO_BUILD, start=1):
    i = len(PLAN) + j
    name = fmt.ytd_sheet_name(year=year)
    time.sleep(SLEEP_BETWEEN_S)
    try:
        if backend.has_worksheet(name):
            skipped.append(name)
            _log(i, name, "· already exists (skipped)")
            continue
        build_ytd_tab(
            backend, fmt,
            year=year,
            categories=categories,
            overwrite=False,
        )
        created.append(name)
        _log(i, name, "✓ created")
    except SheetsError as exc:
        failed.append((name, f"{type(exc).__name__}: {exc}"))
        _log(i, name, f"✗ FAILED — {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        failed.append((name, f"{type(exc).__name__}: {exc}"))
        _log(i, name, f"✗ FAILED — {type(exc).__name__}")

# ─── Final report ──────────────────────────────────────────────────────
elapsed_total = int(time.time() - start)
print()
print(f"═══ DONE in {elapsed_total//60}m {elapsed_total%60}s ═══")
print(f"  Created : {len(created)}")
for n in created:
    print(f"    + {n}")
print(f"  Skipped : {len(skipped)} (already existed)")
for n in skipped:
    print(f"    · {n}")
print(f"  Failed  : {len(failed)}")
for n, err in failed:
    print(f"    ✗ {n}  →  {err}")
print()
sys.exit(1 if failed else 0)
