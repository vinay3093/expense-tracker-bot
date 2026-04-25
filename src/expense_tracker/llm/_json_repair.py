"""Helpers for parsing & lightly repairing LLM-emitted JSON.

LLMs frequently violate the strict JSON spec in three predictable ways
even when explicitly asked for JSON:

1. Wrap the output in markdown fences:    ``` ```json\\n{...}\\n``` ```
2. Add prose before/after the object:    ``"Sure! Here is the JSON: {...}"``
3. Use Unicode "smart" quotes in the keys/values when the underlying
   tokenizer was trained on non-ASCII text.

We strip all three before handing the string to ``json.loads``. We do
NOT attempt aggressive grammar repair (no quoting trailing commas, no
escaping unescaped newlines) — those tend to mask real model failures
that should bubble up so the caller can re-prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel

_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.*?)```",
    flags=re.DOTALL | re.IGNORECASE,
)


def extract_json(text: str) -> str:
    """Strip common LLM wrapping noise from a JSON-bearing string.

    Returns the cleaned string. Does NOT validate it as JSON — that's
    :func:`parse_llm_json`'s job. Designed to be safe (idempotent, never
    raises) so it can be applied unconditionally before parsing.
    """
    cleaned = text.strip()

    # 1. Strip markdown fences if present (with or without "json" tag).
    fence_match = _FENCE_RE.search(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # 2. Trim leading prose by skipping to the first '{'.
    if not cleaned.startswith("{"):
        first = cleaned.find("{")
        if first != -1:
            cleaned = cleaned[first:]

    # 3. Trim trailing prose by cutting after the last '}'.
    if not cleaned.endswith("}"):
        last = cleaned.rfind("}")
        if last != -1:
            cleaned = cleaned[: last + 1]

    # 4. Replace Unicode smart quotes with ASCII ones.
    cleaned = (
        cleaned.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )

    return cleaned


def parse_llm_json(text: str) -> dict[str, Any]:
    """Best-effort parse of an LLM-emitted JSON string.

    Raises :class:`json.JSONDecodeError` if the cleaned version still
    isn't parseable.
    """
    cleaned = extract_json(text)
    return json.loads(cleaned)


_TSchema = TypeVar("_TSchema", bound=BaseModel)


def build_schema_grounding(schema: type[_TSchema]) -> str:
    """Build a system-prompt fragment that tells the model exactly which
    JSON shape we expect.

    Reused by every provider client that supports plain JSON mode (where
    "valid JSON" is enforced but "valid against this schema" is not).
    Anchoring the LLM to a concrete schema reduces the rate of structural
    surprises (extra keys, wrong types, missing required fields).
    """
    schema_json = schema.model_json_schema()
    return (
        "Respond with a single JSON object that conforms exactly to this "
        "JSON Schema. Output JSON only — no prose, no markdown fences, no "
        "comments.\n\nSchema:\n"
        + json.dumps(schema_json, indent=2)
    )
