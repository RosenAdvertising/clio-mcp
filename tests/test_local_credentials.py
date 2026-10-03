"""Local credential precedence, migration, and OAuth allowlist regressions."""

from datetime import datetime, timezone
from types import SimpleNamespace

from clio_mcp import credentials
from clio_mcp.client import stamp_token_payload


def test_app_credentials_resolve_keyring_then_env_then_file(monkeypatch):
    credentials._write_env_file(
        {"CLIO_CLIENT_ID": "file-id", "CLIO_CLIENT_SECRET": "file-secret"}
    )
    monkeypatch.setenv("CLIO_CLIENT_ID", "env-id")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "env-secret")
    values = {"CLIO_CLIENT_ID": "keyring-id", "CLIO_CLIENT_SECRET": "keyring-secret"}
    monkeypatch.setattr(
        credentials,
        "keyring",
        SimpleNamespace(get_password=lambda service, key: values.get(key)),
    )
    monkeypatch.setattr(credentials, "_keyring_enabled", lambda: True)
    pair = credentials.resolve_app_credentials()
    assert (pair.client_id, pair.client_secret) == ("keyring-id", "keyring-secret")
    assert "keyring-secret" not in repr(pair)
    values.clear()
    pair = credentials.resolve_app_credentials()
    assert (pair.client_id, pair.client_secret) == ("env-id", "env-secret")
    monkeypatch.delenv("CLIO_CLIENT_ID")
    monkeypatch.delenv("CLIO_CLIENT_SECRET")
    pair = credentials.resolve_app_credentials()
    assert (pair.client_id, pair.client_secret) == ("file-id", "file-secret")
    assert credentials.ENV_FILE.stat().st_mode & 0o777 == 0o600


def test_token_payload_allowlist_preserves_refresh_and_lifetime():
    timestamp = datetime(2026, 9, 6, tzinfo=timezone.utc)
    result = stamp_token_payload(
        {
            "access_token": "fixture-access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "identity": "private-value",
            "scope": "unexpected",
        },
        previous_refresh_token="fixture-refresh",
        now=timestamp,
    )
    assert result == {
        "access_token": "fixture-access",
        "refresh_token": "fixture-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refreshed_at": timestamp.isoformat(),
        "expires_at": timestamp.timestamp() + 3600,
    }
