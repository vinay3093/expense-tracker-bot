"""Tests for :mod:`expense_tracker.config`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from expense_tracker.config import Settings


def test_default_provider_is_groq(isolated_env) -> None:
    isolated_env()
    s = Settings()
    assert s.LLM_PROVIDER == "groq"
    assert s.GROQ_MODEL == "llama-3.1-8b-instant"
    assert s.OLLAMA_BASE_URL == "http://localhost:11434"


def test_provider_can_be_switched_via_env(isolated_env) -> None:
    isolated_env(LLM_PROVIDER="ollama", OLLAMA_MODEL="llama3.2")
    s = Settings()
    assert s.LLM_PROVIDER == "ollama"
    assert s.OLLAMA_MODEL == "llama3.2"


def test_secret_keys_use_secret_str(isolated_env) -> None:
    """API keys must round-trip through ``SecretStr`` so they don't leak
    into ``repr`` or accidental logging."""
    isolated_env(GROQ_API_KEY="gsk_test_secret_value")
    s = Settings()
    assert s.GROQ_API_KEY is not None
    assert s.GROQ_API_KEY.get_secret_value() == "gsk_test_secret_value"
    assert "gsk_test_secret_value" not in repr(s)


def test_temperature_bounds_are_validated(isolated_env) -> None:
    isolated_env(LLM_TEMPERATURE="3.0")
    with pytest.raises(ValidationError):
        Settings()

    isolated_env(LLM_TEMPERATURE="-0.5")
    with pytest.raises(ValidationError):
        Settings()


def test_retry_bounds_are_validated(isolated_env) -> None:
    isolated_env(LLM_MAX_RETRIES="0")
    with pytest.raises(ValidationError):
        Settings()

    isolated_env(LLM_MAX_RETRIES="11")
    with pytest.raises(ValidationError):
        Settings()


def test_unknown_provider_rejected(isolated_env) -> None:
    isolated_env(LLM_PROVIDER="cohere")
    with pytest.raises(ValidationError):
        Settings()
