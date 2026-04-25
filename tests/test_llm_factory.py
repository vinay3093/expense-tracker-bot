"""Tests for :func:`expense_tracker.llm.factory.get_llm_client`."""

from __future__ import annotations

import pytest

from expense_tracker.config import Settings
from expense_tracker.llm import LLMConfigError, get_llm_client
from expense_tracker.llm._fake import FakeLLMClient


def _settings(**kv: str) -> Settings:
    return Settings(**kv)  # type: ignore[arg-type]


def test_factory_returns_fake_when_provider_is_fake(isolated_env) -> None:
    # Tracing is on by default; disable it here so we can isinstance-check
    # the raw class. Tracing-on behaviour is covered in test_llm_traced.py.
    isolated_env(LLM_PROVIDER="fake", LLM_TRACE="false")
    client = get_llm_client()
    assert isinstance(client, FakeLLMClient)


def test_factory_raises_clean_error_when_groq_key_missing(isolated_env) -> None:
    isolated_env(LLM_PROVIDER="groq")
    with pytest.raises(LLMConfigError) as ei:
        get_llm_client()
    assert "GROQ_API_KEY" in str(ei.value)


def test_factory_raises_clean_error_when_openai_key_missing(isolated_env) -> None:
    isolated_env(LLM_PROVIDER="openai")
    with pytest.raises(LLMConfigError) as ei:
        get_llm_client()
    assert "OPENAI_API_KEY" in str(ei.value)


def test_factory_raises_clean_error_when_anthropic_key_missing(isolated_env) -> None:
    isolated_env(LLM_PROVIDER="anthropic")
    with pytest.raises(LLMConfigError) as ei:
        get_llm_client()
    assert "ANTHROPIC_API_KEY" in str(ei.value)


def test_factory_passes_explicit_settings(isolated_env) -> None:
    isolated_env()
    explicit = _settings(LLM_PROVIDER="fake", LLM_TRACE=False)
    client = get_llm_client(explicit)
    assert isinstance(client, FakeLLMClient)


def test_ollama_construction_does_not_require_a_key(isolated_env) -> None:
    """Ollama runs locally — no API key concept. Just constructs.

    Tracing wrapper mirrors the inner client's identity, so these
    attribute checks pass with or without tracing.
    """
    isolated_env(LLM_PROVIDER="ollama")
    client = get_llm_client()
    assert client.provider_name == "ollama"
    assert client.model == "llama3.1"
