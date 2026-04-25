"""Anthropic (Claude) — opt-in provider.

Not a hard dependency: ``anthropic`` is in ``[project.optional-dependencies]``
and only imported when this provider is actually selected via
``LLM_PROVIDER=anthropic``.

To enable::

    pip install -e ".[anthropic]"
    # then in .env
    LLM_PROVIDER=anthropic
    ANTHROPIC_API_KEY=sk-ant-...
    ANTHROPIC_MODEL=claude-3-5-sonnet-latest

Note: Anthropic's API differs from OpenAI's in two ways we accommodate
here:

1. The system message is a top-level ``system`` parameter, not the first
   item in the ``messages`` list.
2. There is no ``response_format={"type":"json_object"}`` switch — JSON
   mode is achieved by *strong prompt wording* + the schema-grounding
   helper. Validation still happens on our side via Pydantic.
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


class AnthropicClient:
    """Concrete LLM client backed by Anthropic's API."""

    provider_name: ClassVar[str] = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-3-5-sonnet-latest",
        timeout_s: float = 30.0,
        max_retries: int = 3,
        default_temperature: float = 0.1,
        default_max_tokens: int = 1024,
    ) -> None:
        if not api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is empty.")
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise LLMConfigError(
                "The 'anthropic' package is required for the Anthropic provider.\n"
                'Install with: pip install -e ".[anthropic]"'
            ) from exc

        self._client = Anthropic(api_key=api_key, timeout=timeout_s)
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
            extra_system="",
        )

    def complete_json(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, LLMResponse]:
        # Anthropic doesn't have a hard JSON-mode flag — we rely on a
        # strongly-worded system prompt + schema grounding + post-validation.
        resp = self._chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_system=build_schema_grounding(schema),
        )
        try:
            parsed_dict = parse_llm_json(resp.content)
            parsed = schema.model_validate(parsed_dict)
        except Exception as exc:
            raise LLMBadResponseError(
                f"Anthropic returned unparseable JSON for schema "
                f"{schema.__name__}: {resp.content!r}"
            ) from exc
        return parsed, resp

    def _chat(
        self,
        *,
        messages: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        extra_system: str,
    ) -> LLMResponse:
        from anthropic import (
            APIConnectionError,
            APIError,
            APIStatusError,
            RateLimitError,
        )

        # Anthropic separates the system prompt from the messages list.
        system_chunks = [m.content for m in messages if m.role == "system"]
        if extra_system:
            system_chunks.append(extra_system)
        system_prompt = "\n\n".join(system_chunks)
        non_system = [m for m in messages if m.role != "system"]

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
                "max_tokens": (
                    max_tokens if max_tokens is not None else self._default_max_tokens
                ),
                "temperature": (
                    temperature if temperature is not None else self._default_temperature
                ),
                "messages": [m.model_dump() for m in non_system],
            }
            if system_prompt:
                params["system"] = system_prompt

            t0 = time.perf_counter()
            try:
                msg = self._client.messages.create(**params)
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

            # Anthropic responses are a list of content blocks; for plain
            # chat we only ever expect one text block.
            text_parts = [
                getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
            ]
            content = "".join(text_parts)

            usage = getattr(msg, "usage", None)
            prompt_t = getattr(usage, "input_tokens", None) if usage else None
            completion_t = getattr(usage, "output_tokens", None) if usage else None
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
