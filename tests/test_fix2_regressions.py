"""Final regression coverage for portable storage, setup and safe errors."""

import builtins
import errno
import json
import os
import stat
import sys
import traceback
from types import SimpleNamespace

import pytest
import requests

from clio_mcp import client, credentials, files, server, transfers
from clio_mcp.setup import oauth_flow


@pytest.mark.parametrize(
    "entry", [".env", "tokens.json", "credential_state.json", "store.lock"]
)
def test_legacy_modes_are_repaired_on_reads(entry):
    credentials.set_secret("CLIO_CLIENT_ID", "fixture")
    credentials.commit_authorization_grant(
        "local", "local", {"access_token": "fixture"}
    )
    credentials.CONFIG_DIR.chmod(0o755)
    path = credentials.CONFIG_DIR / entry
    path.chmod(0o644)
    assert credentials.get_secret("CLIO_CLIENT_ID") == "fixture"
    assert credentials.get_token_cache("local")
    assert not credentials.credential_is_dead("local")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials.CONFIG_DIR.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "entry", [".env", "tokens.json", "credential_state.json", "store.lock"]
)
def test_private_reads_reject_hardlinks(entry, tmp_path):
    credentials.set_secret("CLIO_CLIENT_ID", "fixture")
    credentials.commit_authorization_grant(
        "local", "local", {"access_token": "fixture"}
    )
    path = credentials.CONFIG_DIR / entry
    os.link(path, tmp_path / "other-link")
    with pytest.raises(credentials.CredentialStoreConfigError):
        credentials._read_private_file(path)


@pytest.mark.parametrize("kind", ["dead", "pending"])
def test_dangling_state_symlink_never_becomes_healthy(kind):
    credentials.commit_authorization_grant(
        "local", "local", {"access_token": "fixture"}
    )
    credentials.mark_credential_dead("local", "fixture")
    if kind == "pending":
        state = credentials._read_credential_state()
        state["users"]["local"]["grant_pending"] = True
        credentials._write_credential_state(state)
    assert credentials.credential_is_dead("local")
    credentials.CREDENTIAL_STATE_FILE.unlink()
    credentials.CREDENTIAL_STATE_FILE.symlink_to(credentials.CONFIG_DIR / "missing")
    for read in [
        lambda: credentials.credential_is_dead("local"),
        lambda: credentials.get_token_cache("local"),
    ]:
        with pytest.raises(credentials.CredentialStoreConfigError):
            read()


def test_owner_and_failed_mode_repair_fail_closed(monkeypatch):
    credentials.set_secret("CLIO_CLIENT_ID", "fixture")
    original = os.fstat

    def wrong_owner(fd):
        info = original(fd)
        return SimpleNamespace(
            st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=info.st_uid + 1
        )

    monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises(credentials.CredentialStoreConfigError, match="owned"):
        credentials.get_secret("CLIO_CLIENT_ID")
    monkeypatch.setattr(os, "fstat", original)
    credentials.ENV_FILE.chmod(0o644)
    monkeypatch.setattr(os, "fchmod", lambda *a: None)
    with pytest.raises(credentials.CredentialStoreConfigError, match="permissions"):
        credentials.get_secret("CLIO_CLIENT_ID")


@pytest.mark.parametrize("use_keyring", [False, True])
def test_windows_surface_credentials_commit_and_document_tools(
    monkeypatch, use_keyring
):
    import fcntl

    original_import = builtins.__import__
    locks = []
    backend = "keyring" if use_keyring else "file"
    if use_keyring:
        values = {}
        monkeypatch.setattr(credentials, "_keyring_enabled", lambda: True)
        monkeypatch.setattr(
            credentials,
            "keyring",
            SimpleNamespace(
                get_password=lambda service, key: values.get(key),
                set_password=lambda service, key, value: values.__setitem__(key, value),
                delete_password=lambda service, key: values.pop(key, None),
            ),
        )

    def locking(fd, mode, count):
        locks.append(mode)
        assert os.fstat(fd).st_size >= 1
        fcntl.flock(fd, fcntl.LOCK_UN if mode == 0 else fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_UNLCK=0, LK_NBLCK=1)
    )

    def windows_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("Windows has no fcntl")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", windows_import)
    for flag in ["O_NOFOLLOW", "O_DIRECTORY", "fchmod"]:
        monkeypatch.delattr(os, flag, raising=False)
    original_open = os.open

    def portable_open(*args, **kwargs):
        assert "dir_fd" not in kwargs
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", portable_open)
    assert credentials.set_secret("CLIO_CLIENT_ID", "fixture") == backend
    credentials.set_secret("CLIO_CLIENT_SECRET", "fixture")
    assert credentials.get_token_cache("local") is None
    assert not credentials.credential_is_dead("local")
    assert (
        credentials.commit_authorization_grant(
            "local", "local", {"access_token": "fixture"}
        )
        == backend
    )
    assert credentials.get_token_cache("local")["access_token"] == "fixture"
    root = files.files_directory()
    (root / "up.bin").write_bytes(b"fixture")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "id": 123,
                    "name": "fixture",
                    "latest_document_version": {
                        "put_url": "https://storage.example/up",
                        "download_url": "https://storage.example/down",
                    },
                }
            }

    monkeypatch.setattr(client.ClioClient, "post", lambda *a, **k: Response())
    monkeypatch.setattr(client.ClioClient, "get", lambda *a, **k: Response())
    monkeypatch.setattr(client.ClioClient, "patch", lambda *a, **k: Response())

    def transfer(method, url, **kwargs):
        if method == "PUT":
            assert kwargs["source"].read() == b"fixture"
        else:
            kwargs["sink"].write(b"downloaded")
        return 10

    monkeypatch.setattr(client, "transfer", transfer)
    assert json.loads(server.upload_document("up.bin"))["data"]["id"] == 123
    assert "Saved to:" in server.download_document(123, "down.bin")
    assert (root / "down.bin").read_bytes() == b"downloaded"
    assert 0 in locks and 1 in locks
    credentials.ENV_FILE.unlink(missing_ok=True)
    credentials.ENV_FILE.symlink_to(root / "up.bin")
    with pytest.raises(credentials.CredentialStoreConfigError):
        credentials._read_private_file(credentials.ENV_FILE)


@pytest.mark.parametrize("explicit", [False, True])
def test_setup_rerun_prompts_fresh_secret_and_defaults_id(
    monkeypatch, capsys, explicit
):
    credentials.set_secret("CLIO_CLIENT_ID", "stored-id")
    credentials.set_secret("CLIO_CLIENT_SECRET", "stored-secret")
    prompts, exchanges = [], []
    monkeypatch.setattr(builtins, "input", lambda prompt: prompts.append(prompt) or "")
    monkeypatch.setattr(
        oauth_flow.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "fresh-secret",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["clio-mcp-setup"] + (["--client-id", "new-id"] if explicit else []),
    )
    monkeypatch.setattr(oauth_flow, "authorize", lambda _: ("code", "verifier"))

    def post(url, **kwargs):
        exchanges.append(kwargs["data"])
        raise requests.ConnectionError("fixture")

    monkeypatch.setattr(oauth_flow.requests, "post", post)
    with pytest.raises(SystemExit):
        oauth_flow.main()
    assert exchanges[0]["client_id"] == ("new-id" if explicit else "stored-id")
    assert exchanges[0]["client_secret"] == "fresh-secret"
    assert any("Secret" in p for p in prompts)
    if not explicit:
        assert "stored-id" in prompts[0]
    assert "stored-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError,
        requests.exceptions.SSLError,
        requests.TooManyRedirects,
        requests.Timeout,
    ],
)
def test_api_and_refresh_errors_suppress_query_context(monkeypatch, failure):
    credentials.set_token_cache(
        "local", "local", {"access_token": "fixture", "refresh_token": "fixture"}
    )
    credentials.set_secret("CLIO_CLIENT_ID", "fixture")
    credentials.set_secret("CLIO_CLIENT_SECRET", "fixture")
    c = client.ClioClient()
    sentinel = "PRIVATE-" + "SENTINEL"

    def fail(*a, **k):
        raise failure("https://api.example/?query=PRIVATE-SENTINEL")

    monkeypatch.setattr(c.session, "request", fail)
    monkeypatch.setattr(client.requests, "post", fail)
    for call, error in [
        (lambda: c.get("contacts", query=sentinel), client.ClioDegradedError),
        (c.tm.refresh, client.TokenRefreshError),
    ]:
        with pytest.raises(error) as caught:
            call()
        assert sentinel not in "".join(traceback.format_exception(caught.value))
        assert caught.value.__suppress_context__


def test_post_response_generation_check_rejects_stale_result(monkeypatch):
    credentials.commit_authorization_grant(
        "local", "local", {"access_token": "fixture"}
    )
    c = client.ClioClient()

    def response(*a, **k):
        credentials.commit_authorization_grant(
            "local", "local", {"access_token": "other"}
        )
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(c.session, "request", response)
    with pytest.raises(client.CredentialChangedError):
        c.get("contacts")


@pytest.mark.parametrize("kind", ["missing", "oversize", "hardlink", "transfer", "api"])
def test_document_tool_errors_are_readable_json(monkeypatch, kind):
    credentials.set_token_cache("local", "local", {"access_token": "fixture"})
    root = files.files_directory()
    (root / "up").write_bytes(b"fixture")
    if kind == "oversize":
        monkeypatch.setenv("CLIO_MCP_MAX_FILE_BYTES", "1")
    if kind == "hardlink":
        os.link(root / "up", root / "other")

    class Response:
        def raise_for_status(self):
            if kind == "api":
                raise requests.HTTPError("https://api.example/?q=PRIVATE-SENTINEL")

        def json(self):
            return {
                "data": {
                    "id": 321,
                    "latest_document_version": {
                        "put_url": "https://storage.example/up"
                    },
                }
            }

    monkeypatch.setattr(client.ClioClient, "post", lambda *a, **k: Response())

    def fail(*a, **k):
        raise transfers.TransferError("Document transfer failed")

    monkeypatch.setattr(client, "transfer", fail)
    result = json.loads(server.upload_document("absent" if kind == "missing" else "up"))
    assert "error" in result and "PRIVATE-SENTINEL" not in result["error"]
    expected = {
        "missing": "file not found",
        "oversize": "exceeds",
        "hardlink": "hard links",
        "transfer": "321",
        "api": "Clio document request failed",
    }
    assert expected[kind] in result["error"]


def test_file_context_preserves_requests_exception():
    root = files.files_directory()
    (root / "up").write_bytes(b"fixture")
    with pytest.raises(requests.ConnectionError):
        with files.open_upload("up"):
            raise requests.ConnectionError("fixture")


def test_download_mode_and_link_unsupported_preserve_no_overwrite(monkeypatch):
    def unsupported(*a, **k):
        raise OSError(errno.ENOTSUP, "fixture")

    monkeypatch.setattr(os, "fchmod", unsupported)
    monkeypatch.setattr(os, "link", unsupported)
    root = files.files_directory()
    with files.download_target("doc") as sink:
        sink.write(b"fixture")
    assert (root / "doc").read_bytes() == b"fixture"
    with pytest.raises(files.DocumentFileError):
        with files.download_target("race") as sink:
            sink.write(b"new")
            (root / "race").write_bytes(b"old")
    assert (root / "race").read_bytes() == b"old"
    assert not list(root.glob(".download-*"))


def test_existing_directory_mode_and_missing_parent_and_absolute_setting(monkeypatch):
    root = files.files_directory()
    root.chmod(0o775)
    assert stat.S_IMODE(files.files_directory().stat().st_mode) == 0o775
    with pytest.raises(files.DocumentFileError, match="folder must exist"):
        files.validate_output("missing/doc")
    monkeypatch.setenv("CLIO_MCP_FILES_DIR", "relative")
    with pytest.raises(files.DocumentFileError, match="must be absolute"):
        files.files_directory()


@pytest.mark.parametrize("ip", ["100.64.0.1", "198.18.0.1"])
def test_vpn_dns_addresses_allowed(ip):
    assert transfers._public(ip)


def test_proxy_skips_dns_but_retains_https_and_environment_ca(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("Proxy destinations must not use local DNS")

    monkeypatch.setattr(socket_module := transfers.socket, "getaddrinfo", forbidden)
    assert socket_module is transfers.socket
    transfers._destination(
        "https://storage.example/file", transfers.time.monotonic() + 1, proxied=True
    )
    with pytest.raises(transfers.TransferError):
        transfers._destination(
            "http://storage.example/file", transfers.time.monotonic() + 1, proxied=True
        )
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/fixture/ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    with requests.Session() as session:
        settings = session.merge_environment_settings(
            "https://storage.example/file", {}, True, None, None
        )
        assert settings["verify"] == "/fixture/ca.pem"
        assert settings["proxies"]["https"] == "http://proxy.example:8080"
    prepared = requests.Request(
        "GET",
        "https://storage.example/file",
        headers={"Authorization": "fixture", "Cookie": "fixture"},
    ).prepare()
    transfers._NoStorageAuth()(prepared)
    assert "Authorization" not in prepared.headers and "Cookie" not in prepared.headers


def test_size_scaled_deadline_and_override(monkeypatch):
    assert transfers._deadline_seconds(100 * 1024 * 1024) == 920
    assert transfers._deadline_seconds(0) == 120
    monkeypatch.setenv("CLIO_MCP_TRANSFER_DEADLINE_SECONDS", "1800")
    assert transfers._deadline_seconds(1) == 1800
    for value in ["0", "nan", "invalid"]:
        monkeypatch.setenv("CLIO_MCP_TRANSFER_DEADLINE_SECONDS", value)
        with pytest.raises(transfers.TransferError):
            transfers._deadline_seconds(1)


@pytest.mark.parametrize("kind", ["api", "transfer"])
def test_download_tool_safe_errors_and_no_partial_file(monkeypatch, kind):
    credentials.set_token_cache("local", "local", {"access_token": "fixture"})

    class Response:
        def raise_for_status(self):
            if kind == "api":
                raise requests.HTTPError("https://api.example/?query=PRIVATE-SENTINEL")

        def json(self):
            return {
                "data": {
                    "id": 123,
                    "latest_document_version": {
                        "download_url": "https://storage.example/down"
                    },
                }
            }

    monkeypatch.setattr(client.ClioClient, "get", lambda *a, **k: Response())

    def fail(*a, **k):
        k["sink"].write(b"partial")
        raise transfers.TransferError("Document transfer failed")

    monkeypatch.setattr(client, "transfer", fail)
    result = json.loads(server.download_document(123, "doc"))
    assert "error" in result and "PRIVATE-SENTINEL" not in result["error"]
    root = files.files_directory()
    assert not (root / "doc").exists() and not list(root.glob(".download-*"))


def test_refresh_json_provider_failure_is_sanitized(monkeypatch):
    credentials.set_token_cache(
        "local", "local", {"access_token": "fixture", "refresh_token": "fixture"}
    )
    credentials.set_secret("CLIO_CLIENT_ID", "fixture")
    credentials.set_secret("CLIO_CLIENT_SECRET", "fixture")

    class Response:
        status_code = 200

        def json(self):
            raise requests.ConnectionError("PRIVATE-SENTINEL")

    monkeypatch.setattr(client.requests, "post", lambda *a, **k: Response())
    with pytest.raises(client.TokenRefreshError) as caught:
        client.TokenManager("local").refresh()
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None


def test_callback_window_and_always_printed_url(monkeypatch, capsys):
    assert oauth_flow.CALLBACK_WAIT_SECONDS == 600

    class Server:
        def __init__(self, *a):
            pass

        def handle_request(self):
            self.code = "fixture-code"
            self.completed = True

        def server_close(self):
            pass

    monkeypatch.setattr(oauth_flow, "_CallbackServer", Server)
    monkeypatch.setattr(oauth_flow.webbrowser, "open", lambda url: True)
    oauth_flow.authorize("fixture-id")
    assert "https://app.clio.com/oauth/authorize?" in capsys.readouterr().out


def test_upload_directory_is_a_safe_file_error():
    root = files.files_directory()
    (root / "folder").mkdir()
    with pytest.raises(files.DocumentFileError, match="regular file"):
        with files.open_upload("folder"):
            pytest.fail("Directory upload opened")


def test_refresh_lock_legacy_mode_is_repaired_under_store_lock(monkeypatch):
    with credentials.credential_transaction("local"):
        pass
    lock = next(credentials.CONFIG_DIR.glob("refresh-*.lock"))
    lock.chmod(0o644)
    original = credentials._verify_private
    checked = []

    def verify(fd):
        held = credentials._process_lock_depth.held
        assert any(name == "store.lock" for _, name in held)
        checked.append(fd)
        original(fd)

    monkeypatch.setattr(credentials, "_verify_private", verify)
    with credentials.credential_transaction("local"):
        pass
    assert checked and stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_document_json_requests_errors_suppress_provider_context():
    class Response:
        def json(self):
            raise requests.ConnectionError("PRIVATE-SENTINEL")

    with pytest.raises(client.ClioDegradedError) as caught:
        client._document_json(Response())
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None
