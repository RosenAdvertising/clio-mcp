"""Offline lifecycle tests for the version-2 per-user credential model."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography.fernet import Fernet

from clio_mcp import client as clio_client
from clio_mcp import credentials


SUB_A = "local-user-a"
SUB_B = "local-user-b"
EMAIL_A = "a@example.com"
EMAIL_B = "b@example.com"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    for name, filename in (
        ("ENV_FILE", ".env"),
        ("TOKEN_FILE", "tokens.json"),
        ("CREDENTIAL_STATE_FILE", "credential_state.json"),
    ):
        monkeypatch.setattr(credentials, name, tmp_path / filename)
    monkeypatch.setattr(credentials, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("CLIO_MCP_USE_KEYRING", "0")
    monkeypatch.setenv("CLIO_MCP_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CLIO_CLIENT_ID", "client-id")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("CLIO_MCP_TOKEN_KEY_RECOVERY", raising=False)
    credentials._refresh_locks.clear()
    clio_client._RATE_LIMITERS.clear()
    yield tmp_path


def _tokens(label: str, *, hours: int = 47) -> dict[str, Any]:
    return {
        "access_token": f"access-{label}",
        "refresh_token": f"refresh-{label}",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp(),
    }


def test_encrypted_multi_user_envelope_round_trip_and_no_plaintext(
    isolated_store,
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))

    assert credentials.credential_subjects() == [SUB_A, SUB_B]
    tokens_a = credentials.get_token_cache(SUB_A)
    tokens_b = credentials.get_token_cache(SUB_B)
    assert tokens_a is not None and tokens_a["access_token"] == "access-a"
    assert tokens_b is not None and tokens_b["refresh_token"] == "refresh-b"
    raw = credentials.TOKEN_FILE.read_bytes()
    assert not raw.startswith(b"{")
    assert b"access-a" not in raw and b"refresh-b" not in raw
    assert os.stat(credentials.TOKEN_FILE).st_mode & 0o777 == 0o600


def test_local_plaintext_legacy_cache_is_wrapped_and_encrypted(isolated_store) -> None:
    legacy = {"access_token": "legacy-access", "refresh_token": "legacy-refresh"}
    credentials.TOKEN_FILE.write_text(json.dumps(legacy))
    credentials.TOKEN_FILE.chmod(0o600)
    assert credentials.get_token_cache(credentials.LOCAL_SUBJECT) == legacy
    assert not credentials.TOKEN_FILE.read_bytes().startswith(b"{")


def test_wrong_key_fails_closed_then_explicitly_quarantines_whole_envelope(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))
    originals = {path: path.read_bytes() for path in (credentials.TOKEN_FILE,)}
    monkeypatch.setenv("CLIO_MCP_TOKEN_KEY", Fernet.generate_key().decode())

    with pytest.raises(credentials.CredentialStoreConfigError, match="Refusing"):
        credentials.credential_subjects()
    with pytest.raises(credentials.CredentialStoreConfigError, match="Refusing"):
        credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("replacement"))
    assert all(path.read_bytes() == original for path, original in originals.items())
    monkeypatch.setenv("CLIO_MCP_TOKEN_KEY_RECOVERY", "1")
    credentials._read_file_token_envelope()
    assert credentials.credential_subjects() == []
    for path, original in originals.items():
        assert not path.exists()
        quarantined = list(isolated_store.glob(f"{path.name}.unreadable-*"))
        assert len(quarantined) == 1 and quarantined[0].read_bytes() == original


def test_b_only_never_falls_back_for_a_and_makes_zero_network_calls(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))
    calls: list[object] = []
    monkeypatch.setattr(
        clio_client.requests.Session,
        "request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    with pytest.raises(
        clio_client.ClioConnectionMissingError, match="No Clio credentials"
    ):
        clio_client.ClioClient(SUB_A)
    assert calls == []
    tokens_b = credentials.get_token_cache(SUB_B)
    assert tokens_b is not None and tokens_b["access_token"] == "access-b"


def test_same_user_double_refresh_is_single_flight(isolated_store, monkeypatch) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("old"))
    calls = 0
    calls_lock = threading.Lock()

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return _tokens("new", hours=720)

    def refresh(_url, *, data, timeout):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return Response()

    monkeypatch.setattr(clio_client.requests, "post", refresh)
    first = clio_client.TokenManager(SUB_A)
    second = clio_client.TokenManager(SUB_A)
    threads = [threading.Thread(target=manager.refresh) for manager in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 1
    tokens_a = credentials.get_token_cache(SUB_A)
    assert tokens_a is not None and tokens_a["access_token"] == "access-new"


def test_a_and_b_refresh_concurrently_without_lost_envelope_writes(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a-old"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b-old"))
    barrier = threading.Barrier(2)

    class Response:
        status_code = 200

        def __init__(self, label: str):
            self.label = label

        def json(self):
            return _tokens(self.label, hours=720)

    def refresh(_url, *, data, timeout):
        barrier.wait(timeout=2)
        label = "a-new" if data["refresh_token"] == "refresh-a-old" else "b-new"
        return Response(label)

    monkeypatch.setattr(clio_client.requests, "post", refresh)
    errors: list[BaseException] = []

    def run(sub: str) -> None:
        try:
            clio_client.TokenManager(sub).refresh()
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(sub,)) for sub in (SUB_A, SUB_B)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    tokens_a = credentials.get_token_cache(SUB_A)
    tokens_b = credentials.get_token_cache(SUB_B)
    assert tokens_a is not None and tokens_a["access_token"] == "access-a-new"
    assert tokens_b is not None and tokens_b["access_token"] == "access-b-new"


def test_callback_a_discards_its_stale_refresh(isolated_store, monkeypatch) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a-old"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b-old"))
    manager_a = clio_client.TokenManager(SUB_A)

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return _tokens("a-stale")

    def callback_a(_url, *, data, timeout):
        credentials.commit_authorization_grant(SUB_A, EMAIL_A, _tokens("a-callback"))
        return Response()

    monkeypatch.setattr(clio_client.requests, "post", callback_a)
    result = manager_a.refresh()
    assert result["access_token"] == "access-a-callback"
    tokens_a = credentials.get_token_cache(SUB_A)
    assert tokens_a is not None and tokens_a["access_token"] == "access-a-callback"


def test_callback_b_does_not_invalidate_refresh_a(isolated_store, monkeypatch) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a-old"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b-old"))
    manager_a = clio_client.TokenManager(SUB_A)

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return _tokens("a-new")

    def callback_b(_url, *, data, timeout):
        credentials.commit_authorization_grant(SUB_B, EMAIL_B, _tokens("b-callback"))
        return Response()

    monkeypatch.setattr(clio_client.requests, "post", callback_b)
    result = manager_a.refresh()
    tokens_a = credentials.get_token_cache(SUB_A)
    tokens_b = credentials.get_token_cache(SUB_B)
    assert result["access_token"] == "access-a-new"
    assert tokens_a is not None and tokens_a["access_token"] == "access-a-new"
    assert tokens_b is not None and tokens_b["access_token"] == "access-b-callback"


def test_credential_state_rejects_unknown_or_malformed_fields(
    isolated_store,
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.CREDENTIAL_STATE_FILE.write_text(
        json.dumps(
            {
                "version": 2,
                "users": {
                    SUB_A: {
                        "email": EMAIL_A,
                        "credential_dead": True,
                        "credential_generation": 1,
                        "unexpected": "fail closed",
                    }
                },
            }
        )
    )
    with pytest.raises(credentials.CredentialStoreConfigError, match="invalid user"):
        credentials.credential_is_dead(SUB_A)


def test_invalid_grant_marks_only_its_generation_dead(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))

    class Invalid:
        status_code = 400

        @staticmethod
        def json():
            return {"error": "invalid_grant"}

    monkeypatch.setattr(clio_client.requests, "post", lambda *a, **k: Invalid())
    with pytest.raises(clio_client.CredentialDeadError):
        clio_client.TokenManager(SUB_A).refresh()
    assert credentials.credential_is_dead(SUB_A)
    assert not credentials.credential_is_dead(SUB_B)


def test_repeated_api_401_is_degraded_without_killing_either_grant(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))

    class Unauthorized:
        status_code = 401
        headers: dict[str, str] = {}

    api = clio_client.ClioClient(SUB_A)
    monkeypatch.setattr(api.session, "request", lambda *a, **k: Unauthorized())

    def successful_refresh():
        api.tm.tokens = _tokens("a-new")
        return api.tm.tokens

    monkeypatch.setattr(api.tm, "refresh", successful_refresh)
    with pytest.raises(clio_client.ClioDegradedError, match="retained"):
        api._request("GET", "users/who_am_i")
    assert not credentials.credential_is_dead(SUB_A)
    assert not credentials.credential_is_dead(SUB_B)


def test_delete_one_user_preserves_other_grant_and_state(isolated_store) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))
    credentials.mark_credential_dead(SUB_A, "invalid")
    credentials.delete_token_cache(SUB_A)
    assert credentials.get_token_cache(SUB_A) is None
    tokens_b = credentials.get_token_cache(SUB_B)
    assert tokens_b is not None and tokens_b["access_token"] == "access-b"
    assert not credentials.credential_is_dead(SUB_A)


@pytest.mark.parametrize("mutation", ["delete", "generation-change"])
def test_request_parked_in_limiter_rechecks_authorization_without_network(
    isolated_store, monkeypatch, mutation
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    client = clio_client.ClioClient(SUB_A)
    admitted = threading.Event()
    release = threading.Event()
    network_calls: list[object] = []
    failures: list[BaseException] = []

    class ParkedLimiter:
        def acquire(self, *, max_wait_seconds):
            admitted.set()
            assert release.wait(timeout=2)
            return True

    monkeypatch.setattr(clio_client, "_rate_limiter_for", lambda _sub: ParkedLimiter())
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *args, **kwargs: network_calls.append((args, kwargs)),
    )

    def send() -> None:
        try:
            client._request("GET", "users/who_am_i")
        except BaseException as exc:  # pragma: no branch - asserted below
            failures.append(exc)

    thread = threading.Thread(target=send)
    thread.start()
    assert admitted.wait(timeout=2)
    if mutation == "delete":
        credentials.delete_token_cache(SUB_A)
    else:
        credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("replacement"))
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], clio_client.CredentialChangedError)
    assert str(failures[0]) == clio_client.CREDENTIAL_CHANGED_MESSAGE
    assert network_calls == []


def test_rate_limiter_buckets_are_shared_per_sub_and_isolated_between_subs(
    isolated_store,
) -> None:
    def clock_a() -> float:
        return 0.0

    limiter_a = clio_client.RollingWindowRateLimiter(
        1, 60, clock=clock_a, sleeper=lambda _: None
    )
    with clio_client._RATE_LIMITERS_LOCK:
        clio_client._RATE_LIMITERS[SUB_A] = limiter_a
    assert clio_client._rate_limiter_for(SUB_A) is limiter_a
    assert clio_client._rate_limiter_for(SUB_A).acquire(max_wait_seconds=1)
    assert not clio_client._rate_limiter_for(SUB_A).acquire(max_wait_seconds=1)
    assert clio_client._rate_limiter_for(SUB_B).acquire(max_wait_seconds=1)
    limiter_b = clio_client._rate_limiter_for(SUB_B)
    clio_client.reset_rate_limiter(SUB_A)
    assert clio_client._rate_limiter_for(SUB_B) is limiter_b
    assert clio_client._rate_limiter_for(SUB_A) is not limiter_a


def test_refresh_and_api_resolve_the_same_subject_bucket(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    seen: list[str] = []

    class Limiter:
        def acquire(self, *, max_wait_seconds):
            seen.append(SUB_A)
            return True

    monkeypatch.setattr(clio_client, "_rate_limiter_for", lambda sub: Limiter())

    class Refresh:
        status_code = 200

        @staticmethod
        def json():
            return _tokens("new")

    monkeypatch.setattr(clio_client.requests, "post", lambda *a, **k: Refresh())
    clio_client.TokenManager(SUB_A).refresh()
    api = clio_client.ClioClient(SUB_A)

    class Ok:
        status_code = 200
        headers: dict[str, str] = {}

    monkeypatch.setattr(api.session, "request", lambda *a, **k: Ok())
    api._request("GET", "users/who_am_i")
    assert seen == [SUB_A, SUB_A]


def test_api_timeouts_and_429_retries_remain_bounded_per_user(
    isolated_store, monkeypatch
) -> None:
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    api = clio_client.ClioClient(SUB_A)
    request_timeouts: list[tuple[float, float]] = []
    sleeps: list[float] = []

    class RateLimited:
        status_code = 429
        headers = {"Retry-After": "9999"}

    def request(*args, **kwargs):
        request_timeouts.append(kwargs["timeout"])
        return RateLimited()

    monkeypatch.setattr(api.session, "request", request)
    monkeypatch.setattr(clio_client.time, "sleep", sleeps.append)
    with pytest.raises(clio_client.ClioDegradedError, match="bounded retries"):
        api._request("GET", "users/who_am_i")
    assert len(request_timeouts) == clio_client.MAX_RATE_LIMIT_RETRIES + 1
    assert all(connect <= 10 and read <= 30 for connect, read in request_timeouts)
    assert sleeps == [30.0] * clio_client.MAX_RATE_LIMIT_RETRIES
    assert not credentials.credential_is_dead(SUB_A)


def test_dead_state_and_clear_preserve_other_local_records(isolated_store):
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("a"))
    credentials.set_token_cache(SUB_B, EMAIL_B, _tokens("b"))
    generation = credentials.credential_generation(SUB_A)
    assert credentials.mark_credential_dead(
        SUB_A, "invalid_grant", expected_generation=generation
    )
    assert credentials.credential_is_dead(SUB_A)
    assert not credentials.credential_is_dead(SUB_B)
    credentials.clear_credential_dead(SUB_A)
    assert not credentials.credential_is_dead(SUB_A)
    credentials.set_token_cache(SUB_A, EMAIL_A, _tokens("replacement"))
    assert not credentials.mark_credential_dead(
        SUB_A, "stale", expected_generation=generation
    )
    assert not credentials.credential_is_dead(SUB_A)


@pytest.mark.parametrize(
    "bad_value",
    [
        {"version": 3, "users": {}},
        {"version": 2, "users": []},
        {"version": 2, "users": {"local": {"tokens": {}}}},
    ],
)
def test_malformed_token_cache_fails_closed_without_overwrite(
    isolated_store, bad_value
):
    original = json.dumps(bad_value).encode()
    credentials.TOKEN_FILE.write_bytes(original)
    with pytest.raises(credentials.CredentialStoreConfigError):
        credentials.get_token_cache(credentials.LOCAL_SUBJECT)
    with pytest.raises(credentials.CredentialStoreConfigError):
        credentials.set_token_cache(
            credentials.LOCAL_SUBJECT, credentials.LOCAL_EMAIL, _tokens("replacement")
        )
    assert credentials.TOKEN_FILE.read_bytes() == original
