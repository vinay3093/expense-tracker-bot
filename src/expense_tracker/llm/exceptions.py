"""Typed LLM exception hierarchy.

A hierarchy (not a single ``LLMError``) lets callers be as specific as
they need:

    try:
        client.complete(...)
    except LLMRateLimitError:
        # back off, requeue
    except LLMConnectionError:
        # retry locally
    except LLMError:
        # everything else LLM-related

The base :class:`LLMError` makes it trivial to catch *any* LLM problem
without having to enumerate subclasses.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base for every LLM-related error this package raises."""


class LLMConfigError(LLMError):
    """Misconfiguration. Examples: provider chosen but API key missing,
    optional SDK not installed for the chosen provider, invalid model name.
    These are user/environment errors — retrying does not help."""


class LLMConnectionError(LLMError):
    """Network-level failure: DNS, refused connection, timeout. Usually
    transient and worth retrying once or twice."""


class LLMRateLimitError(LLMError):
    """Provider returned 429 or an explicit rate-limit signal. Retry with
    exponential backoff."""


class LLMServerError(LLMError):
    """Provider returned 5xx. Often transient, retryable."""


class LLMBadResponseError(LLMError):
    """Provider responded but the payload is unusable: invalid JSON, schema
    mismatch, empty content, etc. Generally NOT retryable from the same
    prompt — caller may want to re-prompt with a corrective message."""
