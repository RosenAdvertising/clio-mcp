"""Offline MCP revision, local negotiation, and registration regressions."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import Client
from mcp.types import LATEST_PROTOCOL_VERSION
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from clio_mcp import server

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_spec_check_pins_the_2026_revision() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "spec_check.py"), "--mcp-only"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Spec check: PASS" in result.stdout
    assert LATEST_PROTOCOL_VERSION == PROTOCOL_VERSION
    assert MODERN_PROTOCOL_VERSIONS == (PROTOCOL_VERSION,)


def test_client_negotiates_modern_by_default_and_keeps_legacy_compatibility() -> None:
    async def negotiate() -> tuple[str, str]:
        async with Client(server.mcp, cache=None) as modern:
            modern_version = modern.protocol_version
        async with Client(server.mcp, mode="legacy", cache=None) as legacy:
            legacy_version = legacy.protocol_version
        return modern_version, legacy_version

    modern_version, legacy_version = asyncio.run(negotiate())
    assert modern_version == PROTOCOL_VERSION
    assert legacy_version == LEGACY_PROTOCOL_VERSION


def test_local_registry_is_deterministic_and_complete():
    async def scenario():
        async with Client(server.mcp, cache=None) as client:
            first = await client.list_tools()
            second = await client.list_tools()
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            return first, second, prompts, resources

    first, second, prompts, resources = asyncio.run(scenario())
    assert [tool.name for tool in first.tools] == [tool.name for tool in second.tools]
    assert len(first.tools) == 30
    assert all(tool.input_schema["type"] == "object" for tool in first.tools)
    assert len(prompts.prompts) == 4
    assert [str(resource.uri) for resource in resources.resources] == [
        "clio://practice_areas"
    ]


def test_missing_credentials_returns_actionable_structured_error(monkeypatch):
    monkeypatch.setattr(server.credentials, "credential_is_dead", lambda _: False)
    monkeypatch.setattr(server.credentials, "get_token_cache", lambda _: None)
    result = asyncio.run(server.mcp.call_tool("list_matters", {}))
    assert result.is_error
    assert result.structured_content == {
        "code": "clio_connection_missing",
        "error": "No Clio credentials are configured. Run clio-mcp-setup.",
    }
    assert json.loads(result.content[0].text) == result.structured_content
