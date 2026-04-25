"""OpenAI (ChatGPT) — opt-in provider.

Not a hard dependency: ``openai`` is in ``[project.optional-dependencies]``
and only imported when this provider is actually selected via
``LLM_PROVIDER=openai``.

To enable::

    pip install -e ".[openai]"
    # then in .env
    LLM_PROVIDER=openai
    OPENAI_API_KEY=sk-...
    OPENAI_MODEL=gpt-4o-mini
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


class OpenAIClient:
    """Concrete LLM client backed by OpenAI's official API."""

    provider_name: ClassVar[str] = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini",
        timeout_s: float = 30.0,
        max_retries: int = 3,
        default_temperature: float = 0.1,
        default_max_tokens: int = 1024,
    ) -> None:
        if not api_key:
            raise LLMConfigError("OPENAI_API_KEY is empty.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMConfigError(
                "The 'openai' package is required for the OpenAI provider.\n"
                'Install with: pip install -e ".[openai]"'
            ) from exc

        self._client = OpenAI(api_key=api_key, timeout=timeout_s)
        self.model = model
        self._timeout_s = timeout_s
        self._max_retries = max(1, max_retries)
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens

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
                f"OpenAI returned unparseable JSON for schema "
                f"{schema.__name__}: {resp.content!r}"
            ) from exc
        return parsed, resp

    def _chat(
        self,
        *,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> LLMResponse:
        from openai import APIConnectionError, APIError, APIStatusError, RateLimitError

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
            except APIError as exc:  # pragma: no cover
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
