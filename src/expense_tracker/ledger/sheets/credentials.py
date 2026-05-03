"""Resolve the path to the Google service-account JSON.

Two sources, in priority order:

1. ``GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`` — the FULL JSON content as
   an env-var string.  Used by hosted deployments (Hugging Face
   Spaces, Render, Koyeb, ...) that don't let you ship a file.  We
   write it to a temp file (chmod 600) and return that path.
2. ``GOOGLE_SERVICE_ACCOUNT_JSON`` — a filesystem path to the JSON
   file on disk.  The original mode, used on laptops + the Oracle
   sheets-edition deploy where you can ``scp`` secrets onto the box.

If both are set the *_CONTENT one wins (the hosted deploy is the
harder model and people are more likely to forget to remove the
unused path env var).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from ...config import Settings, get_settings
from .exceptions import SheetsConfigError

_log = logging.getLogger(__name__)

# Module-level cache — repeated calls in the same process don't
# re-create the temp file (and the on-disk path stays stable for
# diagnostics).
_materialised_path: Path | None = None


def resolve_service_account_path(settings: Settings | None = None) -> str:
    """Return a filesystem path to a service-account JSON file.

    Materialises the JSON from
    ``GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT`` when set; otherwise
    returns ``GOOGLE_SERVICE_ACCOUNT_JSON`` as-is.

    The materialised file lives at
    ``$TMPDIR/expense-tracker-secrets/service-account.json`` with
    mode 600 so the JSON is never readable by other users on the
    host — even on shared platforms like Hugging Face Spaces.

    Idempotent within a process.
    """
    global _materialised_path
    cfg = settings or get_settings()

    if cfg.GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT is not None:
        if _materialised_path is not None and _materialised_path.exists():
            return str(_materialised_path)

        raw = cfg.GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT.get_secret_value().strip()
        if not raw:
            raise SheetsConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT is set but empty.  "
                "Paste the full service-account JSON content (not a path)."
            )
        # Validate parse — fail fast with a clear error rather than
        # letting gspread blow up later with a cryptic auth message.
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SheetsConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT is not valid JSON.  "
                "When pasting into a hosted secret store (Hugging Face "
                "Spaces, Render, etc.), make sure the value isn't "
                "truncated and that newlines inside the private_key are "
                "preserved (literal '\\n' or real newlines both work).  "
                f"Underlying error: {exc}"
            ) from exc
        if not isinstance(parsed, dict) or "private_key" not in parsed:
            raise SheetsConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT parsed but doesn't "
                "look like a service-account JSON (missing 'private_key' "
                "field)."
            )

        # Stable temp dir + atomic write so half-written files are
        # never read by gspread mid-deploy.
        tmpdir = Path(tempfile.gettempdir()) / "expense-tracker-secrets"
        tmpdir.mkdir(mode=0o700, exist_ok=True)
        path = tmpdir / "service-account.json"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".sa-", suffix=".json", dir=str(tmpdir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:  # pragma: no cover — failure mode is rare
            Path(tmp_name).unlink(missing_ok=True)
            raise
        _materialised_path = path
        _log.info(
            "Materialised service-account JSON from env var to %s "
            "(mode 600)", path,
        )
        return str(path)

    if cfg.GOOGLE_SERVICE_ACCOUNT_JSON:
        return cfg.GOOGLE_SERVICE_ACCOUNT_JSON

    raise SheetsConfigError(
        "Neither GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT nor "
        "GOOGLE_SERVICE_ACCOUNT_JSON is set.  For hosted deploys "
        "(Hugging Face Spaces / Render / ...), set the *_CONTENT env "
        "var to the full JSON.  For laptop / VM deploys, set the "
        "path env var to the JSON file."
    )


def reset_for_tests() -> None:
    """Drop the cached temp-file path so the next call re-materialises."""
    global _materialised_path
    if _materialised_path is not None and _materialised_path.exists():
        try:
            _materialised_path.unlink()
        except OSError:  # pragma: no cover
            pass
    _materialised_path = None


__all__ = ["reset_for_tests", "resolve_service_account_path"]
