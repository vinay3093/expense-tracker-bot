"""Authorization for the Telegram front-end.

The bot is *not* meant to be public. It writes into a personal Google
Sheet, so we use an explicit allow-list of Telegram user IDs:

* The list lives in :class:`Settings.TELEGRAM_ALLOWED_USERS` as a
  comma-separated string of integers (e.g. ``"12345678,98765432"``).
* The empty / unset string means *no one is allowed yet* — the bot will
  refuse every message instead of silently allowing access. This is a
  deliberate "secure by default" choice.
* When an unauthorized user messages the bot, the handler replies with a
  short note that *includes their Telegram user ID*. That makes initial
  setup painless: you DM the bot once, the bot tells you your ID, you
  paste it into ``.env``, restart, and you're in.

This module is pure logic — no Telegram SDK imports. Keeps unit tests
fast and means the rest of the codebase can reason about authorization
without pulling in a network library.
"""

from __future__ import annotations

from dataclasses import dataclass


class TelegramAuthError(ValueError):
    """Raised when the configured allow-list is malformed."""


def parse_allowed_users(raw: str | None) -> frozenset[int]:
    """Parse ``"12345,67890"`` into a frozenset of ints.

    Empty / None / whitespace-only input yields an empty frozenset —
    "no one is allowed". Raises :class:`TelegramAuthError` for entries
    that aren't valid integers, so configuration mistakes fail at
    startup with a clear message rather than silently locking everyone
    out.
    """
    if raw is None:
        return frozenset()
    text = raw.strip()
    if not text:
        return frozenset()

    parts = [p.strip() for p in text.split(",")]
    ids: set[int] = set()
    for p in parts:
        if not p:
            continue
        try:
            ids.add(int(p))
        except ValueError as exc:
            raise TelegramAuthError(
                f"TELEGRAM_ALLOWED_USERS contains a non-integer entry: {p!r}. "
                "Expected a comma-separated list of Telegram user IDs."
            ) from exc
    return frozenset(ids)


@dataclass(frozen=True)
class AuthDecision:
    """Result of an auth check on one incoming message."""

    allowed: bool
    user_id: int | None
    reason: str

    @classmethod
    def ok(cls, user_id: int) -> AuthDecision:
        return cls(allowed=True, user_id=user_id, reason="allowed")

    @classmethod
    def deny_no_user(cls) -> AuthDecision:
        return cls(
            allowed=False,
            user_id=None,
            reason="update has no effective_user (channel post / system event)",
        )

    @classmethod
    def deny_not_in_allowlist(cls, user_id: int) -> AuthDecision:
        return cls(
            allowed=False,
            user_id=user_id,
            reason=f"user {user_id} is not in TELEGRAM_ALLOWED_USERS",
        )


class Authorizer:
    """Decides whether a Telegram user may talk to the bot.

    Pure-Python; takes a frozenset of allowed user IDs and answers
    yes/no via :meth:`check`. Designed so the message handler can call
    it without knowing how the allow-list was sourced.
    """

    def __init__(self, allowed: frozenset[int]) -> None:
        self._allowed = allowed

    @property
    def allowed_ids(self) -> frozenset[int]:
        return self._allowed

    @property
    def empty(self) -> bool:
        """True iff nobody is allowed (the secure default)."""
        return len(self._allowed) == 0

    def check(self, user_id: int | None) -> AuthDecision:
        """Decide whether ``user_id`` may use the bot."""
        if user_id is None:
            return AuthDecision.deny_no_user()
        if user_id in self._allowed:
            return AuthDecision.ok(user_id)
        return AuthDecision.deny_not_in_allowlist(user_id)


__all__ = [
    "AuthDecision",
    "Authorizer",
    "TelegramAuthError",
    "parse_allowed_users",
]
