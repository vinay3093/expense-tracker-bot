"""Tests for the Telegram allow-list parser + Authorizer."""

from __future__ import annotations

import pytest

from expense_tracker.telegram_app.auth import (
    AuthDecision,
    Authorizer,
    TelegramAuthError,
    parse_allowed_users,
)

# ─── parse_allowed_users ──────────────────────────────────────────────


def test_parse_allowed_users_none_returns_empty() -> None:
    """Unset env var → frozenset() (deny everyone, secure default)."""
    assert parse_allowed_users(None) == frozenset()


def test_parse_allowed_users_empty_string_returns_empty() -> None:
    """Empty string is treated the same as unset."""
    assert parse_allowed_users("") == frozenset()
    assert parse_allowed_users("   ") == frozenset()


def test_parse_allowed_users_single_id() -> None:
    assert parse_allowed_users("12345") == frozenset({12345})


def test_parse_allowed_users_multiple_ids_with_whitespace() -> None:
    """Whitespace and trailing commas are tolerated — common copy-paste mistakes."""
    assert parse_allowed_users(" 11 , 22, 33 ,") == frozenset({11, 22, 33})


def test_parse_allowed_users_dedupes() -> None:
    """A duplicate ID is not an error; the set silently dedupes."""
    assert parse_allowed_users("99,99,99") == frozenset({99})


def test_parse_allowed_users_rejects_non_integer() -> None:
    """Mistyping an ID should fail loudly so the operator notices."""
    with pytest.raises(TelegramAuthError, match="non-integer"):
        parse_allowed_users("123,not_a_number")


def test_parse_allowed_users_accepts_negative_ids() -> None:
    """Telegram group/channel IDs are negative; allow them through."""
    assert parse_allowed_users("-1001234567890") == frozenset({-1001234567890})


# ─── Authorizer ────────────────────────────────────────────────────────


def test_authorizer_empty_refuses_known_user() -> None:
    """Empty allow-list = refuse everyone (the safe default)."""
    auth = Authorizer(frozenset())
    assert auth.empty is True
    decision = auth.check(12345)
    assert decision.allowed is False
    assert decision.user_id == 12345
    assert "not in TELEGRAM_ALLOWED_USERS" in decision.reason


def test_authorizer_allows_listed_user() -> None:
    auth = Authorizer(frozenset({42, 99}))
    decision = auth.check(42)
    assert decision == AuthDecision.ok(42)
    assert decision.allowed is True


def test_authorizer_rejects_unknown_user() -> None:
    auth = Authorizer(frozenset({42}))
    decision = auth.check(7)
    assert decision.allowed is False
    assert decision.user_id == 7


def test_authorizer_rejects_none_user() -> None:
    """``effective_user`` is None for channel posts — must be denied."""
    auth = Authorizer(frozenset({42}))
    decision = auth.check(None)
    assert decision.allowed is False
    assert decision.user_id is None
    assert "no effective_user" in decision.reason


def test_authorizer_exposes_allowed_ids() -> None:
    """Surface the parsed allow-list so the CLI startup banner can print it."""
    auth = Authorizer(frozenset({1, 2, 3}))
    assert auth.allowed_ids == frozenset({1, 2, 3})
    assert auth.empty is False
