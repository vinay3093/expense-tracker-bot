"""Ollama — local, offline LLM provider.

When to use this:

* You don't have / don't want to use any cloud API key.
* You want to develop on a plane / without internet.
* You're sensitive about chat content leaving your machine.

Endpoint defaults to ``http://localhost:11434`` (Ollama's standard port).
The user must have ``ollama serve`` running and have pulled the chosen
model (``ollama pull llama3.1``).

This client uses ``httpx`` directly because Ollama doesn't ship an
official Python SDK. The wire format is small enough that a thin
JSON-over-HTTP wrapper is clearer than introducing another dep.
"""

from __future__ import annotations

import time
from typing import ClassVar

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ._json_repair import build_schema_grounding, parse_llm_json
from .base import LLMResponse, Message, T
from .exceptions import (
    LLMBadResponseError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMServerError,
)


class OllamaClient:
    """Concrete LLM client backed by a local Ollama daemon."""

    provider_name: ClassVar[str] = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1",
        # Ollama can be slow on first call (model load) — bump the default.
        timeout_s: float = 120.0,
        max_retries: int = 3,
        default_temperature: float = 0.1,
        default_max_tokens: int = 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._timeout_s = timeout_s
        self._max_retries = max(1, max_retries)
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens

    # ─── Public API ──────────────────────────────────────────────────────
    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        return self._chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )

    def complete_json(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, LLMResponse]:
        primed = [Message.system(build_schema_grounding(schema)), *messages]
        resp = self._chat(
            messages=primed,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            parsed_dict = parse_llm_json(resp.content)
            parsed = schema.model_validate(parsed_dict)
        except Exception as exc:
            raise LLMBadResponseError(
                f"Ollama returned unparseable JSON for schema "
                f"{schema.__name__}: {resp.content!r}"
            ) from exc
        return parsed, resp

    # ─── Internal ────────────────────────────────────────────────────────
    def _chat(
        self,
        *,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> LLMResponse:
        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(
                (LLMRateLimitError, LLMServerError, LLMConnectionError)
            ),
            reraise=True,
        )
        def _do_call() -> LLMResponse:
            payload: dict = {
                "model": self.model,
                "messages": [m.model_dump() for m in messages],
                "stream": False,
                "options": {
                    "temperature": (
                        temperature if temperature is not None
                        else self._default_temperature
                    ),
                    "num_predict": (
                        max_tokens if max_tokens is not None
                        else self._default_max_tokens
                    ),
                },
            }
            if json_mode:
                payload["format"] = "json"

            t0 = time.perf_counter()
            try:
                with httpx.Client(timeout=self._timeout_s) as client:
                    r = client.post(f"{self._base_url}/api/chat", json=payload)
            except httpx.ConnectError as exc:
                raise LLMConnectionError(
                    f"Cannot reach Ollama at {self._base_url}. "
                    "Is `ollama serve` running?"
                ) from exc
            except httpx.TimeoutException as exc:
                raise LLMConnectionError(f"Ollama timed out: {exc}") from exc
            latency_ms = (time.perf_counter() - t0) * 1000

            if r.status_code == 429:
                raise LLMRateLimitError(r.text)
            if 500 <= r.status_code < 600:
                raise LLMServerError(f"Ollama HTTP {r.status_code}: {r.text}")
            if r.status_code >= 400:
                raise LLMBadResponseError(f"Ollama HTTP {r.status_code}: {r.text}")

            data = r.json()
            content = (data.get("message") or {}).get("content", "")

            prompt_t = data.get("prompt_eval_count")
            completion_t = data.get("eval_count")
            total_t = (
                (prompt_t or 0) + (completion_t or 0)
                if prompt_t is not None or completion_t is not None
                else None
            )

            return LLMResponse(
                content=content,
                provider=self.provider_name,
                model=self.model,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                total_tokens=total_t,
                latency_ms=latency_ms,
            )

        return _do_call()
