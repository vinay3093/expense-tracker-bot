"""Unit tests for the currency converter.

Network is patched via ``monkeypatch`` on ``httpx.Client.get`` — we never
actually hit Frankfurter from the test suite.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from expense_tracker.ledger.sheets.currency import (
    ConversionResult,
    CurrencyConverter,
    CurrencyError,
    quick_convert_to_primary,
)

# ─── Test helpers ──────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=None,  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def patched_httpx(monkeypatch):
    """Replace ``httpx.Client.get`` so each test controls the response."""
    state: dict = {"calls": [], "responder": None}

    def _set(responder):
        state["responder"] = responder

    def _fake_get(self, url, params=None, **kwargs):
        state["calls"].append((url, dict(params or {})))
        if state["responder"] is None:
            raise RuntimeError("no responder configured")
        return state["responder"](url, params or {})

    monkeypatch.setattr(httpx.Client, "get", _fake_get)
    return _set, state


# ─── Identity / cache hits ─────────────────────────────────────────────

def test_identity_no_network(tmp_path: Path):
    cv = CurrencyConverter(
        primary_currency="USD",
        cache_path=tmp_path / "fx.json",
    )
    res = cv.convert(100.0, "USD")
    assert res.amount == 100.0
    assert res.rate == 1.0
    assert res.source == "identity"


def test_negative_amount_raises(tmp_path: Path):
    cv = CurrencyConverter(cache_path=tmp_path / "fx.json")
    with pytest.raises(CurrencyError):
        cv.convert(-1.0, "USD")


def test_invalid_currency_codes_raise(tmp_path: Path):
    cv = CurrencyConverter(cache_path=tmp_path / "fx.json")
    with pytest.raises(CurrencyError):
        cv.convert(10.0, "EU")
    with pytest.raises(CurrencyError):
        cv.convert(10.0, "USD", to_currency="EU")


def test_cache_hit_returns_without_network(tmp_path: Path, patched_httpx):
    _, state = patched_httpx
    cache_path = tmp_path / "fx.json"
    today = date.today()
    cache_path.write_text(
        json.dumps({today.isoformat(): {"INR_USD": 0.012}}),
        encoding="utf-8",
    )
    cv = CurrencyConverter(primary_currency="USD", cache_path=cache_path)
    res = cv.convert(1000.0, "INR")
    assert res.source == "cache"
    assert res.rate == 0.012
    assert round(res.amount, 4) == 12.0
    assert state["calls"] == []  # no network used


# ─── API path ──────────────────────────────────────────────────────────

def test_api_path_persists_cache(tmp_path: Path, patched_httpx):
    set_responder, state = patched_httpx
    today = date.today()
    set_responder(lambda url, params: _FakeResponse({
        "amount": 1.0,
        "base": params["from"],
        "date": today.isoformat(),
        "rates": {params["to"]: 0.012},
    }))
    cache_path = tmp_path / "fx.json"
    cv = CurrencyConverter(primary_currency="USD", cache_path=cache_path)
    res = cv.convert(1000.0, "INR")
    assert res.source == "api"
    assert res.rate == 0.012
    assert round(res.amount, 4) == 12.0
    assert state["calls"], "expected at least one network call"
    # Cache should now contain the rate.
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    assert raw[today.isoformat()]["INR_USD"] == 0.012


def test_api_path_with_explicit_historical_date(tmp_path: Path, patched_httpx):
    set_responder, state = patched_httpx
    target = date(2025, 1, 15)
    set_responder(lambda url, params: _FakeResponse({
        "amount": 1.0,
        "base": params["from"],
        "date": target.isoformat(),
        "rates": {params["to"]: 0.0115},
    }))
    cv = CurrencyConverter(primary_currency="USD", cache_path=tmp_path / "fx.json")
    res = cv.convert(1000.0, "INR", on_date=target)
    assert res.source == "api"
    assert res.rate_date == target
    # URL should have the date in the path (not /latest).
    url, _ = state["calls"][-1]
    assert target.isoformat() in url


# ─── Stale-cache fallback ──────────────────────────────────────────────

def test_stale_cache_fallback_when_api_fails(tmp_path: Path, patched_httpx):
    set_responder, _ = patched_httpx
    cache_path = tmp_path / "fx.json"
    # Pre-populate the cache with an old entry.
    cache_path.write_text(
        json.dumps({"2025-01-01": {"INR_USD": 0.0118}}),
        encoding="utf-8",
    )

    def _boom(_url, _params):
        raise httpx.ConnectError("network down")

    set_responder(_boom)
    cv = CurrencyConverter(primary_currency="USD", cache_path=cache_path)
    res = cv.convert(1000.0, "INR")
    assert res.source == "stale_cache_fallback"
    assert res.rate == 0.0118
    assert res.rate_date == date(2025, 1, 1)


def test_no_cache_no_network_raises_currency_error(tmp_path: Path, patched_httpx):
    set_responder, _ = patched_httpx

    def _boom(_url, _params):
        raise httpx.ConnectError("network down")

    set_responder(_boom)
    cv = CurrencyConverter(primary_currency="USD", cache_path=tmp_path / "fx.json")
    with pytest.raises(CurrencyError):
        cv.convert(1000.0, "INR")


# ─── quick_convert_to_primary ──────────────────────────────────────────

def test_quick_convert_returns_tuple(tmp_path: Path):
    cv = CurrencyConverter(primary_currency="USD", cache_path=tmp_path / "fx.json")
    amt, rate, rd = quick_convert_to_primary(cv, 50.0, "USD")
    assert amt == 50.0 and rate == 1.0 and isinstance(rd, date)


def test_conversion_result_dataclass_fields():
    r = ConversionResult(
        amount=10.0, rate=2.0, rate_date=date(2026, 4, 24), source="api",
    )
    assert r.amount == 10.0
    assert r.rate == 2.0
    assert r.source == "api"
