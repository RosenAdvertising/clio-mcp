"""In-process Streamable HTTP transport regressions for MCP 2026-07-28.

These tests exercise the stateless HTTP app returned by
``clio_mcp.server.create_serve_app()`` directly over ASGI (no sockets), so
they stay offline like every other test in this suite.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client
from test_list_tool_controls import RecordingClient, page

from clio_mcp import server

PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
ENDPOINT = "/mcp"
# The app binds 127.0.0.1 by default and the SDK auto-enables its loopback
# Host/Origin protection for that bind; a loopback Host with a port passes it.
LOOPBACK_BASE_URL = "http://127.0.0.1:8080"


def _modern_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build one self-contained 2026-07-28 POST (envelope plus routing headers)."""
    request_params = dict(params or {})
    request_params["_meta"] = {
        PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "clio-mcp-http-test", "version": "0"},
    }
    headers = {
        # The app keeps the SDK's SSE default, so both accepts are required.
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": PROTOCOL_VERSION,
        "mcp-method": method,
    }
    if method == "tools/call":
        headers["mcp-name"] = str(request_params["name"])
    elif method == "prompts/get":
        headers["mcp-name"] = str(request_params["name"])
    elif method == "resources/read":
        headers["mcp-name"] = str(request_params["uri"])
    return headers, {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": request_params,
    }


def _post(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    header_overrides: dict[str, str] | None = None,
    base_url: str = LOOPBACK_BASE_URL,
    request_id: int = 1,
) -> httpx2.Response:
    """POST one modern request to a freshly built serve app, in process."""

    async def run() -> httpx2.Response:
        app = server.create_serve_app()
        headers, body = _modern_request(method, params, request_id=request_id)
        if header_overrides:
            headers.update(header_overrides)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url=base_url
            ) as client:
                return await client.post(ENDPOINT, headers=headers, json=body)

    return asyncio.run(run())


def _result(response: httpx2.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    return payload["result"]


async def _stdio_tools() -> list[Any]:
    async with Client(server.mcp, cache=None) as client:
        return (await client.list_tools()).tools


def _stub_vendor(monkeypatch) -> RecordingClient:
    vendor = RecordingClient([page([{"id": 42, "display_number": "M-42"}])])
    monkeypatch.setattr(server, "ClioClient", lambda: vendor)
    monkeypatch.setattr(server.credentials, "credential_is_dead", lambda _: False)
    monkeypatch.setattr(
        server.credentials, "get_token_cache", lambda _: {"access_token": "token"}
    )
    return vendor


def test_practice_area_lookup_does_not_reuse_data_from_previous_credentials(
    monkeypatch,
) -> None:
    class VendorClient:
        user_sub = "local"

        def __init__(self, area_id):
            self.area_id = area_id

        def list_practice_areas(self):
            return {"data": [{"id": self.area_id, "name": "Family"}]}

        def create_matter(self, **kwargs):
            return {"data": kwargs}

    clients = iter((VendorClient(42), VendorClient(7)))
    monkeypatch.setattr(server, "ClioClient", lambda: next(clients))
    first = json.loads(server.create_matter("First", 1, practice_area="Family"))
    second = json.loads(
        server.create_matter("After reauthorization", 1, practice_area="Family")
    )
    assert first["data"]["practice_area_id"] == 42
    assert second["data"]["practice_area_id"] == 7


# ── Stateless single-exchange behavior ──────────────────────────────────────


def test_serve_app_is_stateless_and_keeps_the_sse_default() -> None:
    app = server.create_serve_app()
    manager = server.mcp.session_manager
    assert manager.stateless is True
    assert manager.json_response is False
    assert app is not None


def test_http_tools_list_matches_the_stdio_registry() -> None:
    response = _post("tools/list")
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    http_tools = _result(response)["tools"]
    stdio_tools = asyncio.run(_stdio_tools())
    assert [tool["name"] for tool in http_tools] == [tool.name for tool in stdio_tools]
    assert {tool["name"] for tool in http_tools} == {
        "who_am_i",
        "list_practice_areas",
        "list_users",
        "list_matters",
        "get_matter",
        "create_matter",
        "update_matter",
        "list_contacts",
        "get_contact",
        "create_contact",
        "list_tasks",
        "create_task",
        "update_task",
        "complete_task",
        "list_calendar_entries",
        "create_calendar_entry",
        "list_activities",
        "create_time_entry",
        "create_expense_entry",
        "list_bills",
        "list_documents",
        "upload_document",
        "download_document",
        "list_notes",
        "create_note",
        "list_communications",
        "list_timers",
        "start_timer",
        "stop_timer",
        "create_bill",
    }
    assert [tool["inputSchema"] for tool in http_tools] == [
        tool.input_schema for tool in stdio_tools
    ]


def test_read_tool_runs_end_to_end_over_http(monkeypatch) -> None:
    vendor = _stub_vendor(monkeypatch)
    response = _post("tools/call", {"name": "list_matters", "arguments": {}})
    assert response.status_code == 200, response.text
    result = _result(response)
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {
        "data": [{"id": 42, "display_number": "M-42"}],
        "meta": {"paging": {"next": None}},
    }
    assert vendor.requests[0]["method"] == "GET"
    assert vendor.requests[0]["path"] == "matters"


def test_requests_share_no_session_state_and_never_mint_a_session_id() -> None:
    async def run():
        app = server.create_serve_app()
        manager = server.mcp.session_manager
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=LOOPBACK_BASE_URL
            ) as client:

                async def post(request_id):
                    headers, body = _modern_request("tools/list", request_id=request_id)
                    headers["mcp-session-id"] = f"ignored-{request_id}"
                    body["params"]["_meta"][CLIENT_INFO_META_KEY]["name"] = (
                        f"client-{request_id}"
                    )
                    return await client.post(ENDPOINT, headers=headers, json=body)

                responses = await asyncio.gather(post(11), post(22))
                assert manager._server_instances == {}
                assert manager._session_owners == {}
                return responses

    first, second = asyncio.run(run())
    for response in (first, second):
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers
    assert first.json()["id"] == 11
    assert second.json()["id"] == 22
    assert _result(first)["tools"] == _result(second)["tools"]


def test_client_disconnect_cancels_a_running_tool(monkeypatch) -> None:
    _stub_vendor(monkeypatch)

    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_tool(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        tool = server.mcp._tool_manager.get_tool("list_matters")
        monkeypatch.setattr(tool, "fn", pending_tool)
        monkeypatch.setattr(tool, "is_async", True)
        app = server.create_serve_app()
        headers, body = _modern_request(
            "tools/call", {"name": "list_matters", "arguments": {}}
        )
        request = httpx2.Request("POST", LOOPBACK_BASE_URL + ENDPOINT, headers=headers)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": ENDPOINT,
            "raw_path": ENDPOINT.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8080),
        }
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": json.dumps(body).encode()}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=2)
        assert cancelled.is_set()

    asyncio.run(run())


def test_get_and_delete_on_the_endpoint_are_rejected_with_405() -> None:
    async def run() -> tuple[httpx2.Response, httpx2.Response]:
        app = server.create_serve_app()
        headers, _ = _modern_request("tools/list")
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url=LOOPBACK_BASE_URL
            ) as client:
                get = await client.get(ENDPOINT, headers=headers)
                delete = await client.delete(ENDPOINT, headers=headers)
                return get, delete

    get, delete = asyncio.run(run())
    assert get.status_code == 405
    assert delete.status_code == 405


def test_server_discover_reports_the_modern_version_and_identity() -> None:
    response = _post("server/discover")
    assert response.status_code == 200, response.text
    result = _result(response)
    assert "2026-07-28" in result["supportedVersions"]
    server_info = result["_meta"][SERVER_INFO_META_KEY]
    assert server_info["name"] == "clio-mcp"
    assert server_info["title"]
    assert server_info["version"]


# ── Lifespan behavior in stateless mode ─────────────────────────────────────


def test_stateless_lifespan_runs_once_for_the_app_not_per_request(monkeypatch) -> None:
    lowlevel = server.mcp._lowlevel_server
    original = lowlevel.lifespan
    events: list[str] = []

    @asynccontextmanager
    async def counting_lifespan(app_instance):
        events.append("enter")
        async with original(app_instance) as state:
            yield state
        events.append("exit")

    monkeypatch.setattr(lowlevel, "lifespan", counting_lifespan)

    async def run() -> None:
        app = server.create_serve_app()
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url=LOOPBACK_BASE_URL
            ) as client:
                for request_id in (1, 2, 3):
                    headers, body = _modern_request("tools/list", request_id=request_id)
                    response = await client.post(ENDPOINT, headers=headers, json=body)
                    assert response.status_code == 200, response.text

    asyncio.run(run())
    assert events == ["enter", "exit"]


# ── Transport selection and bind configuration ───────────────────────────────


def test_default_transport_is_stdio(monkeypatch) -> None:
    monkeypatch.delenv("CLIO_MCP_TRANSPORT", raising=False)
    assert server._requested_transport() == "stdio"
    called: list[bool] = []
    monkeypatch.setattr(server.mcp, "run", lambda: called.append(True))
    server.main()
    assert called == [True]


def test_unknown_transport_env_exits_naming_both_options(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_MCP_TRANSPORT", "bogus")
    with pytest.raises(SystemExit) as excinfo:
        server.main()
    message = str(excinfo.value)
    assert "stdio" in message
    assert "streamable-http" in message
    assert "bogus" in message


def test_port_defaults_to_8080_and_rejects_non_integers(monkeypatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    assert server._port() == 8080
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(SystemExit) as excinfo:
        server._port()
    assert "PORT" in str(excinfo.value)
    assert "not-a-port" in str(excinfo.value)


def test_non_loopback_host_requires_allowed_hosts(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_MCP_HOST", "0.0.0.0")
    monkeypatch.delenv("CLIO_MCP_ALLOWED_HOSTS", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        server.create_serve_app()
    assert "CLIO_MCP_ALLOWED_HOSTS" in str(excinfo.value)


def test_loopback_host_relies_on_the_sdk_loopback_protection(monkeypatch) -> None:
    monkeypatch.delenv("CLIO_MCP_HOST", raising=False)
    monkeypatch.delenv("CLIO_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CLIO_MCP_ALLOWED_ORIGINS", raising=False)
    assert server._transport_security() is None

    allowed = _post("tools/list")
    assert allowed.status_code == 200, allowed.text
    refused = _post("tools/list", header_overrides={"origin": "https://evil.example"})
    assert refused.status_code == 403


def test_empty_transport_selects_stdio(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(server.mcp, "run", lambda: called.append(True))
    for value in ("", "   "):
        monkeypatch.setenv("CLIO_MCP_TRANSPORT", value)
        assert server._requested_transport() == "stdio"
        server.main()
    assert called == [True, True]


def test_empty_host_defaults_to_loopback(monkeypatch) -> None:
    for value in ("", " \t "):
        monkeypatch.setenv("CLIO_MCP_HOST", value)
        assert server._host() == "127.0.0.1"


def test_localhost_spelling_is_non_loopback_without_allowed_hosts(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLIO_MCP_HOST", "LOCALHOST")
    monkeypatch.delenv("CLIO_MCP_ALLOWED_HOSTS", raising=False)
    assert server._host() == "LOCALHOST"
    with pytest.raises(SystemExit) as excinfo:
        server.create_serve_app()
    assert "CLIO_MCP_ALLOWED_HOSTS" in str(excinfo.value)


def test_server_import_survives_missing_distribution() -> None:
    import subprocess
    import sys

    script = """
import importlib.metadata
import mcp.server  # httpx2 reads its own metadata while the SDK imports

def missing(distribution):
    raise importlib.metadata.PackageNotFoundError(distribution)

importlib.metadata.version = missing
import clio_mcp.server
print(clio_mcp.server.mcp.name)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "clio-mcp"


def test_non_loopback_bind_enforces_allowed_hosts_and_origins(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("CLIO_MCP_ALLOWED_HOSTS", "clio.example")
    monkeypatch.setenv("CLIO_MCP_ALLOWED_ORIGINS", "https://clio.example")

    allowed = _post(
        "tools/list",
        base_url="http://clio.example",
        header_overrides={"origin": "https://clio.example"},
    )
    assert allowed.status_code == 200, allowed.text

    refused_host = _post(
        "tools/list",
        base_url="http://other.example",
    )
    assert refused_host.status_code == 421

    refused_origin = _post(
        "tools/list",
        base_url="http://clio.example",
        header_overrides={"origin": "https://evil.example"},
    )
    assert refused_origin.status_code == 403
