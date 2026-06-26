"""Retry the 6 ops that 429'd during the main prebuild run.

Uses 120-sec pacing (double the previous run's 60s) so we stay well clear
of the rolling 60-writes/min Sheets quota window.
"""

from __future__ import annotations

import calendar
import sys
import time

from dotenv import load_dotenv

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

# (year, month) for monthly tabs; None means it's the special YTD entry
RETRY_PLAN: list[tuple[int, int | None]] = [
    (2026, 10),   # October 2026
    (2026, 12),   # December 2026
    (2027, 5),    # May 2027
]

SLEEP_BETWEEN_S = 240  # 4 min — bulletproof against any concurrent quota use

cfg = get_settings()
backend = get_sheets_backend(cfg, fake=False)
fmt = get_sheet_format()
categories = get_registry().canonical_names()

print(f"Retry plan: {len(RETRY_PLAN)} ops, {SLEEP_BETWEEN_S}s between")
print(f"Estimated time: ~{len(RETRY_PLAN) * (SLEEP_BETWEEN_S + 12) // 60} min")
print()

start = time.time()
created: list[str] = []
skipped: list[str] = []
failed: list[tuple[str, str]] = []

for i, (year, month) in enumerate(RETRY_PLAN, start=1):
    if month is None:
        name = fmt.ytd_sheet_name(year=year)
        is_ytd = True
    else:
        name = fmt.monthly_sheet_name(
            month_name=calendar.month_name[month],
            month_short=calendar.month_abbr[month],
            month_num=month,
            year=year,
        )
        is_ytd = False

    elapsed = int(time.time() - start)
    print(f"[{i}/{len(RETRY_PLAN)}] +{elapsed:4d}s  {name:18s}  starting...", flush=True)

    try:
        if is_ytd:
            if backend.has_worksheet(name):
                skipped.append(name)
                elapsed = int(time.time() - start)
                print(f"[{i}/{len(RETRY_PLAN)}] +{elapsed:4d}s  {name:18s}  · already exists (skipped)", flush=True)
            else:
                build_ytd_tab(backend, fmt, year=year, categories=categories, overwrite=False)
                created.append(name)
                elapsed = int(time.time() - start)
                print(f"[{i}/{len(RETRY_PLAN)}] +{elapsed:4d}s  {name:18s}  ✓ created", flush=True)
        else:
            build_month_tab(
                backend, fmt,
                year=year, month=month,
                categories=categories,
                overwrite=False,
            )
            created.append(name)
            elapsed = int(time.time() - start)
            print(f"[{i}/{len(RETRY_PLAN)}] +{elapsed:4d}s  {name:18s}  ✓ created", flush=True)
    except SheetsAlreadyExistsError:
        skipped.append(name)
        elapsed = int(time.time() - start)
        print(f"[{i}/{len(RETRY_PLAN)}] +{elapsed:4d}s  {name:18s}  · already exists (skipped)", flush=True)
    except (SheetsError, Exception) as exc:  # noqa: BLE001
        failed.append((name, f"{type(exc).__name__}: {exc}"))
        elapsed = int(time.time() - start)
        print(f"[{i}/{len(RETRY_PLAN)}] +{elapsed:4d}s  {name:18s}  ✗ FAILED — {type(exc).__name__}", flush=True)

    if i < len(RETRY_PLAN):
        time.sleep(SLEEP_BETWEEN_S)

elapsed_total = int(time.time() - start)
print()
print(f"═══ RETRY DONE in {elapsed_total//60}m {elapsed_total%60}s ═══")
print(f"  Created : {len(created)}")
for n in created:
    print(f"    + {n}")
print(f"  Skipped : {len(skipped)}")
for n in skipped:
    print(f"    · {n}")
print(f"  Failed  : {len(failed)}")
for n, err in failed:
    print(f"    ✗ {n}  →  {err}")
sys.exit(1 if failed else 0)
