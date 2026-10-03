"""OAuth callback, setup persistence and least-privilege regressions."""

import base64
import hashlib
import io
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import yaml

from clio_mcp import credentials
from clio_mcp.setup import oauth_flow as oauth


def callback(server, path):
    handler = oauth._CallbackHandler.__new__(oauth._CallbackHandler)
    handler.server = server
    handler.path = path
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    handler.do_GET()
    return statuses[0]


def state_server():
    return SimpleNamespace(
        state="fixture-state", completed=False, code=None, oauth_error=False
    )


@pytest.mark.parametrize(
    "path",
    [
        "/other?code=evil&state=fixture-state",
        "/callback/?code=evil&state=fixture-state",
        "/callback?code=evil",
        "/callback?code=evil&state=wrong",
        "/callback?code=evil&state=fixture-state&state=fixture-state",
        "/callback?code=one&code=two&state=fixture-state",
    ],
)
def test_invalid_callbacks_do_not_consume_authorization(path):
    server = state_server()
    assert callback(server, path) in (400, 404)
    assert not server.completed and server.code is None
    assert callback(server, "/callback?code=valid&state=fixture-state") == 200
    assert server.code == "valid"
    assert callback(server, "/callback?code=replay&state=fixture-state") == 409
    assert server.code == "valid"


def test_callback_uses_constant_time_state_check_and_handles_oauth_error(monkeypatch):
    server = state_server()
    compared = []
    compare = oauth.hmac.compare_digest
    monkeypatch.setattr(
        oauth.hmac,
        "compare_digest",
        lambda a, b: compared.append((a, b)) or compare(a, b),
    )
    assert (
        callback(
            server,
            "/callback?error=access_denied&error_description=untrusted&state=fixture-state",
        )
        == 400
    )
    assert compared == [(b"fixture-state", b"fixture-state")]
    assert server.completed and server.oauth_error and server.code is None


def test_bind_before_browser_pkce_random_state_and_close(monkeypatch):
    order = []
    servers = []
    urls = []

    class Server:
        def __init__(self, address, handler):
            order.append("bind")
            self.requests = 0
            servers.append(self)

        def handle_request(self):
            self.requests += 1
            if self.requests == 1:
                assert callback(self, "/favicon.ico") == 404
            else:
                callback(self, f"/callback?code=valid&state={self.state}")

        def server_close(self):
            order.append("close")

    def browser(url):
        order.append("browser")
        urls.append(url)
        return True

    monkeypatch.setattr(oauth, "_CallbackServer", Server)
    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    grants = [oauth.authorize("fixture-id") for _ in range(2)]
    assert order == ["bind", "browser", "close"] * 2
    assert all(s.requests == 2 for s in servers)
    params = [parse_qs(urlsplit(u).query) for u in urls]
    assert params[0]["state"] != params[1]["state"]
    assert all(len(p["state"][0]) >= 40 for p in params)
    for (_code, verifier), values in zip(grants, params):
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert values["code_challenge"] == [expected]
        assert values["code_challenge_method"] == ["S256"]


@pytest.mark.parametrize("failure", ["timeout", "denied", "browser"])
def test_callback_always_closes_on_failure(monkeypatch, failure):
    closed = []

    class Server:
        def __init__(self, *args):
            pass

        def handle_request(self):
            if failure == "denied":
                callback(self, f"/callback?error=access_denied&state={self.state}")
            else:
                time.sleep(0.003)

        def server_close(self):
            closed.append(True)

    monkeypatch.setattr(oauth, "_CallbackServer", Server)
    monkeypatch.setattr(oauth, "CALLBACK_WAIT_SECONDS", 0.002)

    def browser(url):
        if failure == "browser":
            raise RuntimeError("fixture browser failure")
        return True

    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    with pytest.raises((oauth.OAuthSetupError, RuntimeError)):
        oauth.authorize("fixture-id")
    assert closed == [True]


def test_real_loopback_callback_server(monkeypatch):
    original = oauth._CallbackServer
    server_holder = []

    def create(address, handler):
        server = original(("127.0.0.1", 0), handler)
        server_holder.append(server)
        return server

    errors = []

    def browser(url):
        state = parse_qs(urlsplit(url).query)["state"][0]
        port = server_holder[0].server_address[1]

        def visit():
            try:
                assert (
                    requests.get(
                        f"http://127.0.0.1:{port}/wrong", timeout=2
                    ).status_code
                    == 404
                )
                assert (
                    requests.get(
                        f"http://127.0.0.1:{port}/callback?code=fixture-code&state={state}",
                        timeout=2,
                    ).status_code
                    == 200
                )
            except Exception as exc:
                errors.append(exc)

        threading.Thread(target=visit, daemon=True).start()
        return True

    monkeypatch.setattr(oauth, "_CallbackServer", create)
    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    monkeypatch.setattr(oauth, "CALLBACK_WAIT_SECONDS", 3)
    assert oauth.authorize("fixture-id")[0] == "fixture-code"
    assert errors == []
    assert server_holder[0].socket.fileno() == -1


def test_setup_stamps_allowlist_clears_dead_marker_and_hides_secret(
    monkeypatch, capsys
):
    sub = credentials.LOCAL_SUBJECT
    credentials.set_token_cache(sub, credentials.LOCAL_EMAIL, {"access_token": "old"})
    credentials.mark_credential_dead(sub, "rejected")
    credentials.TOKEN_FILE.unlink()  # Lost cache with a surviving same-generation marker.
    monkeypatch.setattr(sys, "argv", ["clio-mcp-setup", "--client-id", "fixture-id"])
    monkeypatch.setattr(oauth.getpass, "getpass", lambda prompt: "fixture-secret")
    monkeypatch.setattr(
        oauth, "authorize", lambda _: ("fixture-code", "fixture-verifier")
    )
    calls = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def json(self):
            return {
                "access_token": "fixture-access",
                "refresh_token": "fixture-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "unknown": "untrusted",
                "scope": "excess",
            }

    def exchange(url, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(oauth.requests, "post", exchange)
    oauth.main()
    tokens = credentials.get_token_cache(sub)
    assert set(tokens) == {
        "access_token",
        "refresh_token",
        "expires_in",
        "token_type",
        "refreshed_at",
        "expires_at",
    }
    assert not credentials.credential_is_dead(sub)
    assert credentials.credential_generation(sub) == 2
    assert calls[0]["data"]["code_verifier"] == "fixture-verifier"
    assert calls[0]["allow_redirects"] is False
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out + output.err
    assert (
        "Client ID stored (file)" in output.out and "Tokens stored (file)" in output.out
    )


def test_requested_scopes_exactly_cover_registered_endpoint_families():
    import ast

    root = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load((root / "clio_mcp/endpoints.yaml").read_text())
    source = ast.parse((root / "clio_mcp/server.py").read_text())
    methods = set()
    for function in source.body:
        if not isinstance(function, ast.FunctionDef) or not any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "tool"
            for d in function.decorator_list
        ):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                methods.add(node.func.attr)
    endpoint_names = (methods - {"upload_document"}) | {
        "upload_document_create",
        "upload_document_finalize",
    }
    expected = set()
    for endpoint in spec["tools"]:
        if endpoint["name"] in endpoint_names:
            family = endpoint["path"].split("/")[3]
            expected.add(
                family + (".read" if endpoint["method"] == "GET" else ".write")
            )
    assert set(oauth.FULL_SCOPE.split()) == expected
    assert len(oauth.FULL_SCOPE.split()) == len(expected)
    assert "bills.write" not in expected and "trust_accounts.read" not in expected
