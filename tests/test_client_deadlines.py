"""Offline client regressions carried from the September client fixes."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests

from clio_mcp import client as clio_client
from clio_mcp import credentials, server

FAKE_TOKENS = {
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
}
PHASE_BUDGET_SECONDS = 6.0
PHASE_CONNECT_DELAY_SECONDS = 2.0


def _isolate_credential_store(monkeypatch, home):
    for name, filename in (
        ("ENV_FILE", ".env"),
        ("TOKEN_FILE", "tokens.json"),
        ("CREDENTIAL_STATE_FILE", "credential_state.json"),
    ):
        monkeypatch.setattr(credentials, name, home / filename)
    monkeypatch.setattr(credentials, "CONFIG_DIR", home)
    monkeypatch.setenv("CLIO_MCP_USE_KEYRING", "0")


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    _isolate_credential_store(monkeypatch, tmp_path)
    monkeypatch.setenv("CLIO_REQUESTS_PER_MINUTE", "1000")
    clio_client._RATE_LIMITERS.clear()
    clio_client.set_call_deadline(None)
    credentials.set_token_cache(
        credentials.LOCAL_SUBJECT, credentials.LOCAL_EMAIL, FAKE_TOKENS
    )
    yield
    clio_client.set_call_deadline(None)
    clio_client._RATE_LIMITERS.clear()


class _FakeClioHandler(BaseHTTPRequestHandler):
    """Tiny fake Clio: hangs, 429s, 429-then-ok, or answers instantly."""

    behavior: dict[str, Any] = {"mode": "ok", "delay": 0.0, "hits": 0}
    hits_lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        with _FakeClioHandler.hits_lock:
            _FakeClioHandler.behavior["hits"] += 1
            hit_number = _FakeClioHandler.behavior["hits"]
        mode = _FakeClioHandler.behavior["mode"]
        if mode == "hang":
            time.sleep(_FakeClioHandler.behavior["delay"])
            self._respond(200, b'{"data": [], "meta": {}}')
            return
        if mode == "429":
            self._respond(
                429,
                b'{"error": "rate_limited"}',
                extra_headers={"retry-after": "7"},
            )
            return
        if mode == "401":
            self._respond(401, b'{"error": "unauthorized"}')
            return
        if mode == "429_then_ok" and hit_number == 1:
            self._respond(
                429,
                b'{"error": "rate_limited"}',
                extra_headers={"retry-after": "0"},
            )
            return
        self._respond(200, b'{"data": [], "meta": {}}')

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Stand in for Clio's OAuth token endpoint during refresh tests."""
        token_mode = _FakeClioHandler.behavior.get("token_mode", "ok")
        if token_mode == "hang":
            time.sleep(_FakeClioHandler.behavior.get("token_delay", 10.0))
        if token_mode == "500":
            self._respond(500, b'{"error": "server_error"}')
            return
        self._respond(
            200,
            b'{"access_token": "refreshed-token", "refresh_token": "refreshed-r"}',
        )

    def _respond(
        self,
        status: int,
        body: bytes,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def _start_fake_clio(
    monkeypatch,
    mode: str,
    delay: float = 0.0,
    *,
    token_mode: str = "ok",
    token_delay: float = 10.0,
) -> ThreadingHTTPServer:
    _FakeClioHandler.behavior = {
        "mode": mode,
        "delay": delay,
        "hits": 0,
        "token_mode": token_mode,
        "token_delay": token_delay,
    }
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeClioHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(clio_client, "BASE_URL", base)
    monkeypatch.setattr(clio_client, "TOKEN_URL", f"{base}/oauth/token")
    return httpd


def _fake_clio_hits() -> int:
    with _FakeClioHandler.hits_lock:
        return int(_FakeClioHandler.behavior["hits"])


def _call_error_details(result) -> tuple[bool, dict[str, Any] | None]:
    """Direct call_tool results: CallToolResult on error, tuple on success."""
    if hasattr(result, "is_error"):
        if result.is_error:
            return True, result.structured_content
        return False, None
    return False, None  # (unstructured, structured) tuple -> success


def test_stdio_mode_429_still_sleeps_and_retries(monkeypatch, tmp_path) -> None:
    """Stdio mode keeps the historical bounded sleep-and-retry behavior."""
    _isolate_credential_store(monkeypatch, tmp_path)
    monkeypatch.setenv("CLIO_REQUESTS_PER_MINUTE", "1000")
    clio_client._RATE_LIMITERS.clear()
    credentials.set_token_cache(
        credentials.LOCAL_SUBJECT, "local@example.test", FAKE_TOKENS
    )
    fake = _start_fake_clio(monkeypatch, "429_then_ok")

    async def scenario():
        return await server.mcp.call_tool("list_matters", {})

    result = asyncio.run(scenario())
    fake.shutdown()
    hits = _fake_clio_hits()
    clio_client._RATE_LIMITERS.clear()
    assert _call_error_details(result)[0] is False
    assert hits == 2, "stdio mode must still retry the 429 once and then succeed"


def test_token_refresh_timeout_is_clamped_to_the_call_budget(
    local_env, monkeypatch
) -> None:
    """A hanging token endpoint must not outlive the budget under the user lock.

    Measured on the refresh itself, not on the tool call: the async backstop
    would return a response either way, while the worker thread kept holding
    this subject's credential transaction for the full 30s read timeout.
    """
    monkeypatch.setenv("CLIO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "test-client-secret")
    fake = _start_fake_clio(monkeypatch, "401", token_mode="hang", token_delay=30.0)
    try:
        clio_client.set_call_deadline(time.monotonic() + 2.0)
        started = time.monotonic()
        error: Exception | None = None
        try:
            clio_client.TokenManager(credentials.LOCAL_SUBJECT).refresh()
        except Exception as exc:  # noqa: BLE001 - the timing is the assertion
            error = exc
        elapsed = time.monotonic() - started
    finally:
        clio_client.set_call_deadline(None)
        fake.shutdown()
    print(f"REFRESH CLAMP: refresh unwound after {elapsed:.2f}s")
    assert elapsed < 6.0, (
        f"refresh held the credential transaction for {elapsed:.2f}s despite a "
        "2s call budget"
    )
    assert error is not None
    # A timeout is transient: the grant must survive it.
    assert not credentials.credential_is_dead(credentials.LOCAL_SUBJECT)


class _PhaseTimeoutTransport:
    """Mimic how requests spends a (connect, read) tuple: once per phase.

    requests bounds each phase separately, so a slow connect followed by a slow
    read costs connect_delay + read_allowance. This stand-in reproduces that
    accounting exactly, which is what the timeout split has to survive.
    """

    def __init__(self, connect_delay: float, read_delay: float):
        self.connect_delay = connect_delay
        self.read_delay = read_delay
        self.timeouts: list[tuple[float, float]] = []

    def __call__(
        self, *args: Any, timeout: tuple[float, float] = (0.0, 0.0), **kwargs: Any
    ):
        connect_allow, read_allow = timeout
        self.timeouts.append((connect_allow, read_allow))
        time.sleep(min(self.connect_delay, connect_allow))
        if self.connect_delay > connect_allow:
            raise requests.ConnectTimeout("simulated slow connect")
        time.sleep(min(self.read_delay, read_allow))
        if self.read_delay > read_allow:
            raise requests.ReadTimeout("simulated slow read")
        raise AssertionError("this transport is only used for timeout tests")


def test_phase_timeouts_never_exceed_the_remaining_budget() -> None:
    """Connect plus read is bounded by what is left, at every budget size."""
    connect, read = clio_client.phase_timeouts(math.inf)
    assert (connect, read) == (
        float(clio_client.HTTP_CONNECT_TIMEOUT_SECONDS),
        float(clio_client.HTTP_READ_TIMEOUT_SECONDS),
    )
    for remaining in (0.5, 3.0, 6.0, 20.0, 60.0, 300.0):
        connect, read = clio_client.phase_timeouts(remaining)
        assert connect > 0 and read > 0
        assert connect + read <= remaining + 1e-9, (
            f"connect {connect} + read {read} exceeds remaining {remaining}"
        )
        assert connect <= clio_client.HTTP_CONNECT_TIMEOUT_SECONDS
        assert read <= clio_client.HTTP_READ_TIMEOUT_SECONDS


def test_slow_connect_then_slow_read_stays_inside_the_budget(
    local_env, monkeypatch
) -> None:
    """A slow connect followed by a slow read must not outlast the call budget."""
    transport = _PhaseTimeoutTransport(
        connect_delay=PHASE_CONNECT_DELAY_SECONDS, read_delay=60.0
    )
    monkeypatch.setattr(requests.Session, "request", transport)
    client = clio_client.ClioClient(credentials.LOCAL_SUBJECT)

    clio_client.set_call_deadline(time.monotonic() + PHASE_BUDGET_SECONDS)
    started = time.monotonic()
    error: Exception | None = None
    try:
        client.get("matters")
    except Exception as exc:  # noqa: BLE001 - the elapsed time is the assertion
        error = exc
    finally:
        elapsed = time.monotonic() - started
        clio_client.set_call_deadline(None)

    print(f"PHASE SPLIT api: returned after {elapsed:.2f}s")
    assert error is not None
    assert elapsed <= PHASE_BUDGET_SECONDS, (
        f"connect plus read ran {elapsed:.2f}s against a {PHASE_BUDGET_SECONDS}s budget"
    )
    connect_allow, read_allow = transport.timeouts[0]
    assert connect_allow + read_allow <= PHASE_BUDGET_SECONDS + 1e-9


def test_slow_connect_then_slow_read_stays_inside_the_budget_on_refresh(
    local_env, monkeypatch
) -> None:
    """Same bound while the refresh holds this subject's credential transaction."""
    monkeypatch.setenv("CLIO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "test-client-secret")
    transport = _PhaseTimeoutTransport(
        connect_delay=PHASE_CONNECT_DELAY_SECONDS, read_delay=60.0
    )
    monkeypatch.setattr(clio_client.requests, "post", transport)

    clio_client.set_call_deadline(time.monotonic() + PHASE_BUDGET_SECONDS)
    started = time.monotonic()
    error: Exception | None = None
    try:
        clio_client.TokenManager(credentials.LOCAL_SUBJECT).refresh()
    except Exception as exc:  # noqa: BLE001 - the elapsed time is the assertion
        error = exc
    finally:
        elapsed = time.monotonic() - started
        clio_client.set_call_deadline(None)

    print(f"PHASE SPLIT refresh: returned after {elapsed:.2f}s")
    assert error is not None
    assert elapsed <= PHASE_BUDGET_SECONDS, (
        f"refresh held the credential transaction {elapsed:.2f}s against a "
        f"{PHASE_BUDGET_SECONDS}s budget"
    )
    connect_allow, read_allow = transport.timeouts[0]
    assert connect_allow + read_allow <= PHASE_BUDGET_SECONDS + 1e-9
    # A timeout is transient: the grant must survive it.
    assert not credentials.credential_is_dead(credentials.LOCAL_SUBJECT)


class _NeverCalledTransport:
    """Records any attempt to open a connection; the tests require none."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any):
        self.calls += 1
        raise AssertionError("an HTTP call was made with the budget already spent")


def test_sub_millisecond_budget_refuses_to_open_a_connection(
    local_env, monkeypatch
) -> None:
    """Below the floor no timeout split is honest, so the call never starts.

    Covers both HTTP sites: the API request path and the token refresh.
    """
    monkeypatch.setenv("CLIO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "test-client-secret")
    api_transport = _NeverCalledTransport()
    refresh_transport = _NeverCalledTransport()
    client = clio_client.ClioClient(credentials.LOCAL_SUBJECT)
    token_manager = clio_client.TokenManager(credentials.LOCAL_SUBJECT)
    monkeypatch.setattr(requests.Session, "request", api_transport)
    monkeypatch.setattr(clio_client.requests, "post", refresh_transport)

    try:
        clio_client.set_call_deadline(time.monotonic() + 0.001)
        with pytest.raises(clio_client.ClioCallBudgetExceededError):
            client.get("matters")

        clio_client.set_call_deadline(time.monotonic() + 0.001)
        with pytest.raises(clio_client.ClioCallBudgetExceededError):
            token_manager.refresh()
    finally:
        clio_client.set_call_deadline(None)

    assert api_transport.calls == 0
    assert refresh_transport.calls == 0
    # The grant survives: running out of budget is not a credential problem.
    assert not credentials.credential_is_dead(credentials.LOCAL_SUBJECT)


def test_phase_timeouts_floor_matches_the_refusal_threshold() -> None:
    """Anything the split still accepts must fit inside the remaining budget."""
    floor = clio_client.MIN_HTTP_BUDGET_SECONDS
    for remaining in (floor, floor * 2, 0.01, 0.1):
        connect, read = clio_client.phase_timeouts(remaining)
        assert connect + read <= remaining + 1e-9
