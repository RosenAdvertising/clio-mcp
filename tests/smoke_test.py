#!/usr/bin/env python3
"""
Clio MCP smoke test — full-stack, in-process.

Calls every no-param read tool through the MCP protocol layer (MCPServer →
Client → ClioClient → Clio API). Catches field expansion bugs, auth
failures, and server.py wiring errors before any client install.

Usage:
    python3 tests/smoke_test.py

Requires sandbox credentials at ~/.clio-mcp/.env and tokens.json.
Write tests are skipped unless ALLOW_WRITE_TESTS=1 is set.

Exit code: 0 = all tested tools passed, 1 = any failure.
"""

import asyncio
import json
import os
import sys
from mcp import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from clio_mcp.server import mcp

ALLOW_WRITE = os.environ.get("ALLOW_WRITE_TESTS") == "1"

# Tools callable with no arguments (all list/read operations).
# These are the mandatory smoke tests — every release must pass all of them.
READ_TOOLS = [
    "who_am_i",
    "list_practice_areas",
    "list_users",
    "list_matters",
    "list_contacts",
    "list_tasks",
    "list_calendar_entries",
    "list_activities",
    "list_bills",
    "list_documents",
    "list_notes",
    "list_communications",
    "list_timers",
]

# Write tools — only run when ALLOW_WRITE_TESTS=1 and sandbox IDs are set.
# Each entry: (tool_name, args_dict)
# Set env vars SMOKE_MATTER_ID and SMOKE_CONTACT_ID to enable these.
_MATTER_ID = int(os.environ.get("SMOKE_MATTER_ID", "0"))
_CONTACT_ID = int(os.environ.get("SMOKE_CONTACT_ID", "0"))

WRITE_TOOLS = []
if _MATTER_ID:
    WRITE_TOOLS += [
        ("get_matter", {"matter_id": _MATTER_ID}),
        ("list_tasks", {"matter_id": _MATTER_ID}),
        ("list_notes", {"matter_id": _MATTER_ID}),
        ("list_activities", {"matter_id": _MATTER_ID}),
    ]
if _CONTACT_ID:
    WRITE_TOOLS += [
        ("get_contact", {"contact_id": _CONTACT_ID}),
    ]


def _check_result(content_text: str) -> tuple[bool, str]:
    """Return (passed, reason). Fails if body contains error.type."""
    try:
        body = json.loads(content_text)
    except json.JSONDecodeError:
        return False, f"invalid JSON: {content_text[:120]}"
    if isinstance(body, dict) and body.get("error", {}).get("type"):
        return (
            False,
            f"error.type={body['error']['type']}: {body['error'].get('message', '')}",
        )
    return True, "ok"


async def run_smoke():
    results = []

    tools_to_run = [(t, {}) for t in READ_TOOLS]
    if ALLOW_WRITE and WRITE_TOOLS:
        tools_to_run += WRITE_TOOLS

    async with Client(mcp, read_timeout_seconds=30) as session:
        for tool_name, args in tools_to_run:
            try:
                result = await session.call_tool(tool_name, args)
                if result.is_error:
                    passed, reason = False, f"MCP error: {result.content}"
                elif not result.content:
                    passed, reason = False, "empty response"
                else:
                    text = getattr(result.content[0], "text", "")
                    passed, reason = _check_result(text)
            except Exception as e:
                passed, reason = False, str(e)

            results.append({"tool": tool_name, "passed": passed, "reason": reason})

    return results


def print_report(results: list[dict]) -> bool:
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]

    print(f"\nClio MCP smoke test — {len(results)} tools\n")
    print(f"  PASS: {len(passed)}")
    print(f"  FAIL: {len(failed)}")
    print()

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        line = f"  [{status}] {r['tool']}"
        if not r["passed"]:
            line += f"\n         → {r['reason']}"
        print(line)

    if failed:
        print(f"\n{len(failed)} tool(s) failed. Fix before releasing.\n")
    else:
        print("\nAll tools passed.\n")

    return len(failed) == 0


if __name__ == "__main__":
    results = asyncio.run(run_smoke())
    all_passed = print_report(results)
    sys.exit(0 if all_passed else 1)
