"""Groq Cloud — primary LLM provider.

Why Groq for this project:

* Free tier with generous rate limits — ideal for a personal bot.
* Very low latency (Groq specializes in fast inference).
* OpenAI-compatible API surface, so the same JSON-mode incantations
  used by ChatGPT just work.

Implementation uses the official ``groq`` Python SDK. The SDK call is
wrapped in:

1. Time measurement (``time.perf_counter``) for ``LLMResponse.latency_ms``.
2. Exception mapping — Groq's SDK exceptions are translated into our own
   :mod:`~expense_tracker.llm.exceptions` hierarchy so callers don't have
   to import groq.
3. ``tenacity`` retry-with-exponential-backoff on transient failures
   (rate-limit, 5xx, network), no retry on misuse errors (4xx, config).
"""

from __future__ import annotations

import time
from typing import ClassVar

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
    LLMConfigError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMServerError,
)


class GroqClient:
    """Concrete LLM client backed by Groq Cloud."""

    provider_name: ClassVar[str] = "groq"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "llama-3.1-8b-instant",
        timeout_s: float = 30.0,
        max_retries: int = 3,
        default_temperature: float = 0.1,
        default_max_tokens: int = 1024,
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                "GROQ_API_KEY is empty. Set it in .env or the environment."
            )
        try:
            from groq import Groq
        except ImportError as exc:  # pragma: no cover — required dep
            raise LLMConfigError(
                "The 'groq' package is required for the Groq provider.\n"
                "Install with: pip install groq"
            ) from exc

        self._client = Groq(api_key=api_key, timeout=timeout_s)
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
        # Inject the JSON schema as an extra system message. Groq's JSON
        # mode only enforces *valid JSON*, not the shape — schema grounding
        # in the prompt closes that gap.
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
                f"Groq returned unparseable JSON for schema "
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
        # Lazy import — groq SDK exceptions are only available once the SDK
        # is installed AND we're actually about to call.
        from groq import APIConnectionError, APIError, APIStatusError, RateLimitError

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(
                (LLMRateLimitError, LLMServerError, LLMConnectionError)
            ),
            reraise=True,
        )
        def _do_call() -> LLMResponse:
            params: dict = {
                "model": self.model,
                "messages": [m.model_dump() for m in messages],
                "temperature": (
                    temperature if temperature is not None else self._default_temperature
                ),
                "max_tokens": (
                    max_tokens if max_tokens is not None else self._default_max_tokens
                ),
            }
            if json_mode:
                params["response_format"] = {"type": "json_object"}

            t0 = time.perf_counter()
            try:
                completion = self._client.chat.completions.create(**params)
            except RateLimitError as exc:
                raise LLMRateLimitError(str(exc)) from exc
            except APIConnectionError as exc:
                raise LLMConnectionError(str(exc)) from exc
            except APIStatusError as exc:
                code = getattr(exc, "status_code", None)
                if code is not None and 500 <= int(code) < 600:
                    raise LLMServerError(str(exc)) from exc
                raise LLMBadResponseError(str(exc)) from exc
            except APIError as exc:  # pragma: no cover — fallback branch
                raise LLMBadResponseError(str(exc)) from exc
            latency_ms = (time.perf_counter() - t0) * 1000

            choice = completion.choices[0]
            usage = completion.usage
            return LLMResponse(
                content=choice.message.content or "",
                provider=self.provider_name,
                model=self.model,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                latency_ms=latency_ms,
            )

        return _do_call()
