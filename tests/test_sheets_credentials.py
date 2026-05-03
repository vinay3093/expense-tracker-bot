"""Unit tests for the service-account credentials resolver.

Two source paths exist (file path vs raw JSON content env var) and the
resolver has to fail fast on misconfiguration.  These tests cover all
five branches without ever talking to Google.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from expense_tracker.config import get_settings
from expense_tracker.ledger.sheets.credentials import (
    reset_for_tests,
    resolve_service_account_path,
)
from expense_tracker.ledger.sheets.exceptions import SheetsConfigError

# A minimal, syntactically-valid service-account JSON.  Includes the
# ``private_key`` field the resolver sanity-checks for.  Not a real
# key — Google would reject it, but gspread isn't being called here.
_FAKE_SA_JSON: dict[str, str] = {
    "type": "service_account",
    "project_id": "fake-project",
    "private_key_id": "deadbeef",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "fake@fake-project.iam.gserviceaccount.com",
    "client_id": "1",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://example.com/x509",
}


@pytest.fixture(autouse=True)
def _clear_creds_cache() -> None:
    """Drop the module-level cache before every test.

    The resolver caches the materialised path so repeated calls in
    one process re-use the same temp file.  Without this fixture,
    the second test in a run would short-circuit and miss the new
    env var.
    """
    reset_for_tests()
    yield
    reset_for_tests()


def test_path_env_var_returned_as_is(isolated_env, tmp_path):
    sa = tmp_path / "sa.json"
    sa.write_text(json.dumps(_FAKE_SA_JSON), encoding="utf-8")
    isolated_env(GOOGLE_SERVICE_ACCOUNT_JSON=str(sa))

    resolved = resolve_service_account_path(get_settings())

    assert resolved == str(sa)


def test_content_env_var_materialises_to_temp_file(isolated_env):
    isolated_env(
        GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=json.dumps(_FAKE_SA_JSON),
    )

    resolved = resolve_service_account_path(get_settings())

    assert resolved.endswith("service-account.json")
    body = Path(resolved).read_text(encoding="utf-8")
    assert json.loads(body) == _FAKE_SA_JSON


def test_content_env_var_writes_file_with_mode_600(isolated_env):
    isolated_env(
        GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=json.dumps(_FAKE_SA_JSON),
    )

    resolved = resolve_service_account_path(get_settings())

    mode = stat.S_IMODE(os.stat(resolved).st_mode)
    # Owner read+write only — no group, no world.  Critical on
    # multi-tenant hosts like Hugging Face Spaces.
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_content_env_var_wins_over_path(isolated_env, tmp_path):
    """When both env vars are set, _CONTENT takes precedence."""
    sa_path = tmp_path / "old-sa.json"
    sa_path.write_text("{}", encoding="utf-8")
    isolated_env(
        GOOGLE_SERVICE_ACCOUNT_JSON=str(sa_path),
        GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=json.dumps(_FAKE_SA_JSON),
    )

    resolved = resolve_service_account_path(get_settings())

    assert resolved != str(sa_path)
    assert json.loads(Path(resolved).read_text(encoding="utf-8")) == _FAKE_SA_JSON


def test_neither_env_var_set_raises(isolated_env):
    isolated_env()

    with pytest.raises(SheetsConfigError) as exc:
        resolve_service_account_path(get_settings())

    msg = str(exc.value)
    # Error must name BOTH env vars so the user can fix it without
    # reading source.
    assert "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT" in msg
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in msg


def test_content_env_var_empty_raises(isolated_env):
    isolated_env(GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT="   ")

    with pytest.raises(SheetsConfigError, match="empty"):
        resolve_service_account_path(get_settings())


def test_content_env_var_invalid_json_raises(isolated_env):
    isolated_env(GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT="this { is not [ json")

    with pytest.raises(SheetsConfigError, match="not valid JSON"):
        resolve_service_account_path(get_settings())


def test_content_env_var_missing_private_key_raises(isolated_env):
    truncated = {"type": "service_account", "client_email": "x@y"}
    isolated_env(
        GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=json.dumps(truncated),
    )

    with pytest.raises(SheetsConfigError, match="private_key"):
        resolve_service_account_path(get_settings())


def test_content_env_var_idempotent_within_process(isolated_env):
    """Repeated calls return the same path — the temp file is cached."""
    isolated_env(
        GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=json.dumps(_FAKE_SA_JSON),
    )

    first = resolve_service_account_path(get_settings())
    second = resolve_service_account_path(get_settings())
    third = resolve_service_account_path(get_settings())

    assert first == second == third
    assert Path(first).exists()
