from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from mcp.server.mcpserver.exceptions import ToolError

from clio_mcp import server
from clio_mcp.client import BASE_URL, ClioClient


LIST_TOOLS = (
    "list_matters",
    "list_contacts",
    "list_tasks",
    "list_calendar_entries",
    "list_activities",
)
ORDER_CASES = (
    ("list_matters", "created_at(desc)"),
    ("list_contacts", "shared_at(desc)"),
    ("list_tasks", "due_at(desc)"),
    ("list_activities", "date(desc)"),
)


class FakeResponse:
    def __init__(self, body: dict[str, Any]):
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class RecordingClient(ClioClient):
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def _request(
        self, method, path, params=None, json_body=None, retry=True
    ) -> FakeResponse:
        self.requests.append({"method": method, "path": path, "params": params})
        return FakeResponse(next(self.responses))


def page(data: list[dict[str, Any]], next_url: str | None = None) -> dict[str, Any]:
    return {"data": data, "meta": {"paging": {"next": next_url}}}


def tool_schemas() -> dict[str, dict[str, Any]]:
    return {
        tool.name: tool.input_schema
        for tool in asyncio.run(server.mcp.list_tools())
        if tool.name in LIST_TOOLS
    }


def query_for(request: dict[str, Any]) -> dict[str, list[str]]:
    url = (
        requests.Request(
            request["method"],
            f"{BASE_URL}/{request['path']}",
            params=request["params"],
        )
        .prepare()
        .url
    )
    assert url is not None
    return parse_qs(urlsplit(url).query)


def test_list_tool_schemas_expose_bounded_limit_and_documented_order() -> None:
    schemas = tool_schemas()

    assert set(schemas) == set(LIST_TOOLS)
    for name in LIST_TOOLS:
        limit = schemas[name]["properties"]["limit"]
        assert limit["default"] == 25
        assert limit["minimum"] == 1
        assert limit["maximum"] == 200

    for name, supported_value in ORDER_CASES:
        order = schemas[name]["properties"]["order"]
        assert order["default"] == "id(asc)"
        assert supported_value in order["enum"]

    assert set(schemas["list_contacts"]["properties"]["order"]["enum"]) == {
        "id(asc)",
        "id(desc)",
        "name(asc)",
        "name(desc)",
        "shared_at(asc)",
        "shared_at(desc)",
    }
    assert "order" not in schemas["list_calendar_entries"]["properties"]


@pytest.mark.parametrize(
    ("method_name", "kwargs", "expected_order"),
    [
        (
            "list_matters",
            {"limit": 10, "order": "created_at(desc)"},
            "created_at(desc)",
        ),
        ("list_contacts", {"limit": 10, "order": "shared_at(desc)"}, "shared_at(desc)"),
        ("list_tasks", {"limit": 10, "order": "due_at(desc)"}, "due_at(desc)"),
        ("list_calendar_entries", {"limit": 10}, None),
        ("list_activities", {"limit": 10, "order": "date(desc)"}, "date(desc)"),
    ],
)
def test_list_request_url_contains_limit_and_supported_order(
    method_name: str, kwargs: dict[str, Any], expected_order: str | None
) -> None:
    client = RecordingClient([page([])])

    getattr(client, method_name)(**kwargs)

    query = query_for(client.requests[0])
    assert query["limit"] == ["10"]
    if expected_order is None:
        assert "order" not in query
    else:
        assert query["order"] == [expected_order]


@pytest.mark.parametrize("method_name", LIST_TOOLS)
def test_list_request_defaults_to_twenty_five_records(method_name: str) -> None:
    client = RecordingClient([page([])])

    getattr(client, method_name)()

    query = query_for(client.requests[0])
    assert query["limit"] == ["25"]
    if method_name == "list_calendar_entries":
        assert "order" not in query
    else:
        assert query["order"] == ["id(asc)"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        *((name, {"limit": 0}) for name in LIST_TOOLS),
        *((name, {"limit": 201}) for name in LIST_TOOLS),
        *((name, {"order": "not_a_clio_order(desc)"}) for name, _ in ORDER_CASES),
    ],
)
async def test_list_tool_rejects_invalid_limit_and_order(
    tool_name: str, arguments: dict[str, Any]
) -> None:
    tool = server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None

    with pytest.raises(ToolError, match="validation error"):
        await tool.run(arguments, None)


def test_contact_limit_is_total_records_not_auto_pagination_page_size() -> None:
    next_url = f"{BASE_URL}/contacts.json?page_token=second-page"
    client = RecordingClient(
        [page([{"id": record_id} for record_id in range(1, 11)], next_url)]
    )

    result = client.list_contacts(limit=10, order="id(desc)")

    assert len(result["data"]) == 10
    assert len(client.requests) == 1
    assert result["truncated"] is True
