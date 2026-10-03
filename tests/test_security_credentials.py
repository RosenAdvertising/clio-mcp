"""Private writes, keyring downgrade and cross-process token transactions."""

import json
import multiprocessing
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_mcp import client, credentials


def test_env_atomic_private_creation_with_permissive_umask(monkeypatch):
    observed = []
    original = credentials.os.open

    def opened(path, flags, mode=0o777, **kwargs):
        fd = original(path, flags, mode, **kwargs)
        if flags & os.O_CREAT:
            observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    monkeypatch.setattr(credentials.os, "open", opened)
    old = os.umask(0)
    try:
        assert credentials.set_secret("CLIO_CLIENT_SECRET", "fixture-secret") == "file"
    finally:
        os.umask(old)
    assert observed and all(mode == 0o600 for mode in observed)
    assert stat.S_IMODE(credentials.ENV_FILE.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials.CONFIG_DIR.stat().st_mode) == 0o700


def test_env_symlink_and_permission_failure_fail_closed(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.write_text("do not change")
    credentials.CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials.ENV_FILE.symlink_to(outside)
    with pytest.raises(credentials.CredentialStoreConfigError):
        credentials.set_secret("CLIO_CLIENT_SECRET", "fixture-secret")
    assert outside.read_text() == "do not change"
    credentials.ENV_FILE.unlink()

    def denied(*a):
        raise PermissionError("fixture chmod denied")

    monkeypatch.setattr(credentials.os, "fchmod", denied)
    with pytest.raises(PermissionError):
        credentials.set_secret("CLIO_CLIENT_SECRET", "fixture-secret")
    assert not credentials.ENV_FILE.exists()


def fake_keyring(monkeypatch):
    data = {}
    backend = SimpleNamespace(writable=True)

    def get(service, key):
        return data.get(key)

    def set_password(service, key, value):
        if not backend.writable:
            raise RuntimeError("fixture secret in backend exception")
        data[key] = value

    def delete(service, key):
        data.pop(key, None)

    monkeypatch.setattr(
        credentials,
        "keyring",
        SimpleNamespace(
            get_password=get, set_password=set_password, delete_password=delete
        ),
    )
    monkeypatch.setattr(credentials, "_keyring_enabled", lambda: True)
    return data, backend


def test_keyring_failure_reports_fallback_and_no_stale_token_shadow(
    monkeypatch, caplog
):
    data, backend = fake_keyring(monkeypatch)
    sub = credentials.LOCAL_SUBJECT
    assert (
        credentials.set_token_cache(
            sub, credentials.LOCAL_EMAIL, {"access_token": "old"}
        )
        == "keyring"
    )
    generation = credentials.credential_generation(sub)
    backend.writable = False
    replaced, used, new_generation = credentials.replace_token_cache_if_generation(
        sub, {"access_token": "new"}, generation
    )
    assert replaced and used == "file" and new_generation == generation + 1
    assert credentials.get_token_cache(sub) == {"access_token": "new"}
    assert credentials.credential_generation(sub) == new_generation
    assert (
        "OS keyring write failed; falling back to private local file storage"
        in caplog.text
    )
    assert "fixture secret" not in caplog.text
    assert credentials._TOKEN_CACHE_KEY not in data
    assert credentials.set_secret("CLIO_CLIENT_SECRET", "fixture-new-secret") == "file"
    assert credentials.get_secret("CLIO_CLIENT_SECRET") == "fixture-new-secret"
    assert "fixture-new-secret" not in caplog.text


def test_fallback_refuses_undeletable_old_keyring_value(monkeypatch):
    data, backend = fake_keyring(monkeypatch)
    sub = credentials.LOCAL_SUBJECT
    credentials.set_token_cache(sub, credentials.LOCAL_EMAIL, {"access_token": "old"})
    backend.writable = False
    monkeypatch.setattr(credentials.keyring, "delete_password", lambda *a: None)
    with pytest.raises(
        credentials.CredentialStoreConfigError, match="safely fall back"
    ):
        credentials.set_token_cache(
            sub, credentials.LOCAL_EMAIL, {"access_token": "new"}
        )
    assert not credentials.TOKEN_FILE.exists()
    assert credentials.get_token_cache(sub) == {"access_token": "old"}


def configure_child(directory):
    directory = Path(directory)
    credentials.CONFIG_DIR = directory
    credentials.ENV_FILE = directory / ".env"
    credentials.TOKEN_FILE = directory / "tokens.json"
    credentials.CREDENTIAL_STATE_FILE = directory / "credential_state.json"
    os.environ["CLIO_MCP_USE_KEYRING"] = "0"
    os.environ.pop("CLIO_MCP_TOKEN_KEY", None)
    os.environ["CLIO_CLIENT_ID"] = "fixture-id"
    os.environ["CLIO_CLIENT_SECRET"] = "fixture-secret"


def cas_worker(directory, barrier, results):
    configure_child(directory)
    generation = credentials.credential_generation(credentials.LOCAL_SUBJECT)
    barrier.wait(timeout=5)
    result = credentials.replace_token_cache_if_generation(
        credentials.LOCAL_SUBJECT, {"access_token": "replacement"}, generation
    )
    results.put(result)


def refresh_worker(directory, barrier, calls, results):
    configure_child(directory)

    class Response:
        status_code = 200

        def json(self):
            return {"access_token": "new", "refresh_token": "rotated"}

    def post(*a, **kw):
        with calls.get_lock():
            calls.value += 1
        time.sleep(0.05)
        return Response()

    client.requests.post = post
    manager = client.TokenManager(credentials.LOCAL_SUBJECT)
    barrier.wait(timeout=5)
    results.put(manager.refresh()["access_token"])


def run_processes(worker, args):
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    barrier = ctx.Barrier(2)
    children = [
        ctx.Process(
            target=worker, args=(str(credentials.CONFIG_DIR), barrier, *args, results)
        )
        for _ in range(2)
    ]
    for process in children:
        process.start()
    try:
        for process in children:
            process.join(timeout=10)
            assert not process.is_alive() and process.exitcode == 0
        return [results.get(timeout=2) for _ in children]
    finally:
        for process in children:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
        results.close()


def test_multiprocess_generation_compare_and_write_is_atomic():
    sub = credentials.LOCAL_SUBJECT
    credentials.set_token_cache(sub, credentials.LOCAL_EMAIL, {"access_token": "old"})
    results = run_processes(cas_worker, ())
    assert sorted(result[0] for result in results) == [False, True]
    assert credentials.credential_generation(sub) == 2
    assert stat.S_IMODE((credentials.CONFIG_DIR / "store.lock").stat().st_mode) == 0o600


def test_multiprocess_refresh_rotates_once_and_adopts_winner():
    sub = credentials.LOCAL_SUBJECT
    credentials.set_token_cache(
        sub,
        credentials.LOCAL_EMAIL,
        {"access_token": "old", "refresh_token": "old-refresh"},
    )
    ctx = multiprocessing.get_context("spawn")
    calls = ctx.Value("i", 0)
    assert run_processes(refresh_worker, (calls,)) == ["new", "new"]
    assert calls.value == 1
    assert credentials.credential_generation(sub) == 2
    assert credentials.get_token_cache(sub)["refresh_token"] == "rotated"
    assert all(
        stat.S_IMODE(p.stat().st_mode) == 0o600
        for p in credentials.CONFIG_DIR.glob("*.lock")
    )


def test_local_legacy_alert_state_remains_readable_without_new_alert_keys():
    sub = credentials.LOCAL_SUBJECT
    credentials.set_token_cache(
        sub, credentials.LOCAL_EMAIL, {"access_token": "fixture"}
    )
    credentials.CREDENTIAL_STATE_FILE.write_text(
        json.dumps(
            {
                "version": 2,
                "users": {
                    sub: {
                        "email": "local",
                        "credential_dead": True,
                        "credential_generation": 1,
                        "last_alert_at": "2026-01-01",
                        "alert_attempt_count": 1,
                        "alert_failure_count": 0,
                    }
                },
            }
        )
    )
    assert credentials.credential_is_dead(sub)
    credentials.clear_credential_dead(sub)
    record = json.loads(credentials.CREDENTIAL_STATE_FILE.read_text())["users"][sub]
    assert record == {
        "email": "local",
        "credential_dead": False,
        "credential_generation": 1,
    }
