"""Currency conversion to the spreadsheet's primary currency.

Why this lives in the Sheets layer:

* The Transactions tab stores BOTH the original amount + a converted
  amount in the primary currency (USD by default). Monthly + YTD
  formulas SUM the converted column — no live FX in the Sheet.
* The conversion rate gets recorded in the row so the math is auditable
  and reproducible long after the API changes.

Source: https://www.frankfurter.app — free, no API key required, ECB
reference rates updated ~16:00 CET on bank business days.

Failure model:

* Same currency: identity (rate=1.0), no API call.
* Cached rate for the date: returned immediately.
* Network call: fetch + cache + return.
* Network fails / API down: fall back to the most recent cached rate
  for that pair (warns to stderr). NEVER blocks an expense write —
  the user's chat experience must stay responsive even if FX is down.
* No cached rate at all: raises :class:`CurrencyError`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

_FRANKFURTER_BASE = "https://api.frankfurter.app"


class CurrencyError(Exception):
    """Raised when conversion fails and no fallback rate is available."""


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of one ``convert`` call."""

    amount: float           # converted amount in `to_currency`
    rate: float             # `to_currency` per 1 unit of `from_currency`
    rate_date: date         # the date the rate is valid for
    source: str             # "identity", "cache", "api", "stale_cache_fallback"


# ─── Cache file format ──────────────────────────────────────────────────

def _cache_key(from_ccy: str, to_ccy: str) -> str:
    return f"{from_ccy.upper()}_{to_ccy.upper()}"


@dataclass
class _RateCache:
    """JSON-on-disk cache, ``{date_iso: {pair_key: rate}}``."""

    path: Path
    _data: dict[str, dict[str, float]] | None = None

    def _load(self) -> dict[str, dict[str, float]]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            self._data = {}
            return self._data
        try:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}
        return self._data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)

    def get(self, on_date: date, from_ccy: str, to_ccy: str) -> float | None:
        data = self._load()
        return data.get(on_date.isoformat(), {}).get(_cache_key(from_ccy, to_ccy))

    def put(self, on_date: date, from_ccy: str, to_ccy: str, rate: float) -> None:
        data = self._load()
        bucket = data.setdefault(on_date.isoformat(), {})
        bucket[_cache_key(from_ccy, to_ccy)] = rate
        self._save()

    def latest(self, from_ccy: str, to_ccy: str) -> tuple[date, float] | None:
        """Return the most recent cached rate for the pair, if any."""
        data = self._load()
        key = _cache_key(from_ccy, to_ccy)
        candidates: list[tuple[date, float]] = []
        for date_str, rates in data.items():
            if key in rates:
                try:
                    candidates.append((date.fromisoformat(date_str), rates[key]))
                except ValueError:
                    continue
        if not candidates:
            return None
        return max(candidates, key=lambda x: x[0])


# ─── Currency converter ─────────────────────────────────────────────────

class CurrencyConverter:
    """Convert any amount to the primary currency.

    Designed for use during chat → Transactions write. Constructed once
    per process; safe to share across requests (no mutable state beyond
    the on-disk cache, which is JSON-flushed after each successful API
    call).
    """

    def __init__(
        self,
        *,
        primary_currency: str = "USD",
        cache_path: str | Path = "./logs/fx_cache.json",
        timeout_s: float = 5.0,
        api_base: str = _FRANKFURTER_BASE,
    ) -> None:
        self._primary = primary_currency.upper()
        self._cache = _RateCache(path=Path(cache_path))
        self._timeout_s = timeout_s
        self._api_base = api_base.rstrip("/")

    @property
    def primary_currency(self) -> str:
        return self._primary

    # ─── Public API ─────────────────────────────────────────────────────
    def convert(
        self,
        amount: float,
        from_currency: str,
        *,
        to_currency: str | None = None,
        on_date: date | None = None,
    ) -> ConversionResult:
        """Return ``amount`` in ``to_currency`` (defaults to primary)."""
        if amount < 0:
            raise CurrencyError(f"amount must be non-negative, got {amount}")

        from_ccy = from_currency.upper().strip()
        to_ccy = (to_currency or self._primary).upper().strip()
        if not from_ccy or len(from_ccy) != 3:
            raise CurrencyError(f"invalid from_currency: {from_currency!r}")
        if not to_ccy or len(to_ccy) != 3:
            raise CurrencyError(f"invalid to_currency: {to_currency!r}")

        if from_ccy == to_ccy:
            return ConversionResult(
                amount=amount,
                rate=1.0,
                rate_date=on_date or date.today(),
                source="identity",
            )

        target_date = on_date or date.today()

        cached = self._cache.get(target_date, from_ccy, to_ccy)
        if cached is not None:
            return ConversionResult(
                amount=round(amount * cached, 6),
                rate=cached,
                rate_date=target_date,
                source="cache",
            )

        # API call (with retries).
        try:
            rate, rate_date = self._fetch_rate(from_ccy, to_ccy, target_date)
        except Exception as exc:
            stale = self._cache.latest(from_ccy, to_ccy)
            if stale is not None:
                stale_date, stale_rate = stale
                print(
                    f"[currency] {from_ccy}->{to_ccy} API failed ({exc}); "
                    f"falling back to cached rate from {stale_date}",
                    file=sys.stderr,
                )
                return ConversionResult(
                    amount=round(amount * stale_rate, 6),
                    rate=stale_rate,
                    rate_date=stale_date,
                    source="stale_cache_fallback",
                )
            raise CurrencyError(
                f"could not convert {from_ccy}->{to_ccy}: API failed and no "
                f"cached rate is available ({exc})"
            ) from exc

        # Cache successful fetch under the requested date even if Frankfurter
        # returned a slightly older date (weekend / holiday) — that way a
        # repeat call for the same target_date is instant.
        self._cache.put(target_date, from_ccy, to_ccy, rate)
        if rate_date != target_date:
            self._cache.put(rate_date, from_ccy, to_ccy, rate)

        return ConversionResult(
            amount=round(amount * rate, 6),
            rate=rate,
            rate_date=rate_date,
            source="api",
        )

    # ─── HTTP layer ─────────────────────────────────────────────────────
    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
    )
    def _fetch_rate(
        self, from_ccy: str, to_ccy: str, target_date: date
    ) -> tuple[float, date]:
        """Hit Frankfurter once and return (rate, effective_date).

        Frankfurter URL forms used:
          - ``/latest?from=X&to=Y`` for today.
          - ``/<date>?from=X&to=Y`` for historical / weekend roll-back.
        """
        # If target_date is in the future, fall back to /latest to avoid
        # a 422 from the API.
        today = date.today()
        if target_date > today:
            target_date = today

        if target_date == today:
            url = f"{self._api_base}/latest"
        else:
            url = f"{self._api_base}/{target_date.isoformat()}"
        params = {"from": from_ccy, "to": to_ccy}

        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        rates = data.get("rates", {})
        rate = rates.get(to_ccy)
        if rate is None:
            raise CurrencyError(
                f"Frankfurter returned no rate for {to_ccy} "
                f"in response: {data!r}"
            )
        try:
            effective = date.fromisoformat(str(data.get("date", target_date.isoformat())))
        except ValueError:
            effective = target_date
        return float(rate), effective


# ─── Convenience factory ────────────────────────────────────────────────

def get_converter(
    *,
    primary_currency: str = "USD",
    log_dir: str | Path = "./logs",
    timeout_s: float = 5.0,
) -> CurrencyConverter:
    """Construct a :class:`CurrencyConverter` rooted at ``log_dir``."""
    return CurrencyConverter(
        primary_currency=primary_currency,
        cache_path=Path(log_dir) / "fx_cache.json",
        timeout_s=timeout_s,
    )


# Convenience helper for callers that just want a flat tuple back.
def quick_convert_to_primary(
    converter: CurrencyConverter,
    amount: float,
    from_currency: str,
    on_date: date | None = None,
) -> tuple[float, float, date]:
    """Return ``(amount_primary, rate, rate_date)`` — order matches Transactions row."""
    res = converter.convert(amount, from_currency, on_date=on_date)
    return res.amount, res.rate, res.rate_date
