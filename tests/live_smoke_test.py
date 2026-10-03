#!/usr/bin/env python3
"""
Clio MCP live smoke test — full-stack against real Clio API.

Runs every tool through the MCP protocol layer using real credentials.
Catches schema bugs, field errors, and response-shape failures.

Usage (read-only):
    python3 tests/live_smoke_test.py

Usage (write tests):
    ALLOW_WRITE_TESTS=1 python3 tests/live_smoke_test.py

Credentials read from ~/.clio-mcp/.env and ~/.clio-mcp/tokens.json
No IDs or credentials stored in repo.
Write test records are prefixed [MCP SMOKE].
"""

import asyncio
import json
import os
import sys
from datetime import date

from mcp import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from clio_mcp.server import mcp

ALLOW_WRITE = os.environ.get("ALLOW_WRITE_TESTS") == "1"
PREFIX = "[MCP SMOKE]"
TODAY = date.today().isoformat()

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


def parse_json_response(result):
    if result.is_error:
        raise ValueError(f"MCP error: {result.content}")
    if not result.content:
        raise ValueError("empty response")
    text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {text[:120]}") from exc


def assert_no_error(body):
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        if err.get("type"):
            raise ValueError(f"error.type={err['type']}: {err.get('message', '')}")


def extract_data_id(body):
    if isinstance(body, dict):
        data = body.get("data", body)
        if isinstance(data, dict):
            return data.get("id")
    return None


def _check_read(result):
    body = parse_json_response(result)
    assert_no_error(body)
    return "ok"


async def _run_write_tests(session):
    results = []
    generated = {}

    contact_id = None
    matter_id = None
    task_id = None
    timer_id = None

    # create_contact
    try:
        result = await session.call_tool(
            "create_contact",
            {
                "contact_type": "Person",
                "first_name": PREFIX,
                "last_name": "Contact",
                "email": "mcp-smoke-test@example.com",
            },
        )
        body = parse_json_response(result)
        assert_no_error(body)
        contact_id = extract_data_id(body)
        if not contact_id:
            raise ValueError("no id returned")
        generated["contact_id"] = contact_id
        results.append({"tool": "create_contact", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "create_contact", "passed": False, "reason": str(e)})
        return results, generated

    # create_matter
    try:
        result = await session.call_tool(
            "create_matter",
            {
                "description": f"{PREFIX} Matter",
                "client_id": contact_id,
                "status": "Open",
                "billing_method": "hourly",
            },
        )
        body = parse_json_response(result)
        assert_no_error(body)
        matter_id = extract_data_id(body)
        if not matter_id:
            raise ValueError("no id returned")
        generated["matter_id"] = matter_id
        results.append({"tool": "create_matter", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "create_matter", "passed": False, "reason": str(e)})
        return results, generated

    # create_task
    try:
        result = await session.call_tool(
            "create_task",
            {
                "name": f"{PREFIX} Task",
                "matter_id": matter_id,
                "due_at": TODAY,
                "priority": "Normal",
            },
        )
        body = parse_json_response(result)
        assert_no_error(body)
        task_id = extract_data_id(body)
        if not task_id:
            raise ValueError("no id returned")
        generated["task_id"] = task_id
        results.append({"tool": "create_task", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "create_task", "passed": False, "reason": str(e)})

    # complete_task
    if task_id:
        try:
            result = await session.call_tool("complete_task", {"task_id": task_id})
            body = parse_json_response(result)
            assert_no_error(body)
            results.append({"tool": "complete_task", "passed": True, "reason": "ok"})
        except Exception as e:
            results.append({"tool": "complete_task", "passed": False, "reason": str(e)})
    else:
        results.append(
            {
                "tool": "complete_task",
                "passed": False,
                "reason": "skipped: create_task failed",
            }
        )

    # create_note
    try:
        result = await session.call_tool(
            "create_note",
            {
                "subject": f"{PREFIX} Note",
                "detail": "Live smoke test note",
                "matter_id": matter_id,
            },
        )
        body = parse_json_response(result)
        assert_no_error(body)
        if not extract_data_id(body):
            raise ValueError(f"no id returned: {json.dumps(body)[:300]}")
        results.append({"tool": "create_note", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "create_note", "passed": False, "reason": str(e)})

    # create_time_entry
    try:
        result = await session.call_tool(
            "create_time_entry",
            {
                "matter_id": matter_id,
                "date": TODAY,
                "hours": 1.0,
                "note": f"{PREFIX} time entry",
                "rate": 200.0,
            },
        )
        body = parse_json_response(result)
        assert_no_error(body)
        if not extract_data_id(body):
            raise ValueError(f"no id returned: {json.dumps(body)[:300]}")
        results.append({"tool": "create_time_entry", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "create_time_entry", "passed": False, "reason": str(e)})

    # create_expense_entry
    try:
        result = await session.call_tool(
            "create_expense_entry",
            {
                "matter_id": matter_id,
                "amount": 50.00,
                "description": f"{PREFIX} expense",
                "date": TODAY,
            },
        )
        body = parse_json_response(result)
        assert_no_error(body)
        if not extract_data_id(body):
            raise ValueError(f"no id returned: {json.dumps(body)[:300]}")
        results.append({"tool": "create_expense_entry", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append(
            {"tool": "create_expense_entry", "passed": False, "reason": str(e)}
        )

    # create_bill: Clio API v4 does not support bill creation
    try:
        result = await session.call_tool(
            "create_bill",
            {
                "matter_id": matter_id,
                "subject": f"{PREFIX} Invoice",
                "issued_at": TODAY,
                "due_at": TODAY,
            },
        )
        body = parse_json_response(result)
        if isinstance(body, dict) and body.get("status") == "not_supported":
            results.append(
                {
                    "tool": "create_bill",
                    "passed": True,
                    "reason": "not_supported (Clio API limitation)",
                }
            )
        else:
            assert_no_error(body)
            if not extract_data_id(body):
                raise ValueError(f"no id returned: {json.dumps(body)[:600]}")
            results.append({"tool": "create_bill", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "create_bill", "passed": False, "reason": str(e)})

    # start_timer
    try:
        result = await session.call_tool("start_timer", {"matter_id": matter_id})
        body = parse_json_response(result)
        assert_no_error(body)
        timer_id = extract_data_id(body)
        if not timer_id:
            raise ValueError("no id returned")
        generated["timer_id"] = timer_id
        results.append({"tool": "start_timer", "passed": True, "reason": "ok"})
    except Exception as e:
        results.append({"tool": "start_timer", "passed": False, "reason": str(e)})

    # stop_timer
    if timer_id:
        try:
            result = await session.call_tool("stop_timer", {"timer_id": timer_id})
            if result.is_error:
                raise ValueError(f"MCP error: {result.content}")
            if result.content:
                text = result.content[0].text
                try:
                    body = json.loads(text)
                    assert_no_error(body)
                except json.JSONDecodeError:
                    pass  # plain text response is acceptable per spec
            results.append({"tool": "stop_timer", "passed": True, "reason": "ok"})
        except Exception as e:
            results.append({"tool": "stop_timer", "passed": False, "reason": str(e)})
    else:
        results.append(
            {
                "tool": "stop_timer",
                "passed": False,
                "reason": "skipped: start_timer failed",
            }
        )

    return results, generated


async def run_tests():
    results = []
    generated = {}

    async with Client(mcp, read_timeout_seconds=30) as session:
        for tool_name in READ_TOOLS:
            try:
                result = await session.call_tool(tool_name, {})
                reason = _check_read(result)
                results.append({"tool": tool_name, "passed": True, "reason": reason})
            except Exception as e:
                results.append({"tool": tool_name, "passed": False, "reason": str(e)})

        if ALLOW_WRITE:
            write_results, generated = await _run_write_tests(session)
            results.extend(write_results)

    return results, generated


def print_report(results, generated):
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]

    mode = "write" if ALLOW_WRITE else "read-only"
    print(f"\nClio MCP live smoke test ({mode}) — {len(results)} tools\n")
    print(f"  PASS: {len(passed)}")
    print(f"  FAIL: {len(failed)}")
    print()

    col_w = max(len(r["tool"]) for r in results) + 2
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        line = f"  [{status}] {r['tool']:{col_w}}"
        if not r["passed"]:
            line += f" → {r['reason']}"
        print(line)

    if ALLOW_WRITE and generated:
        print("\nGenerated IDs (not written to disk):")
        for key, val in generated.items():
            print(f"  {key}: {val}")

    if failed:
        print(f"\n{len(failed)} tool(s) failed.\n")
    else:
        print("\nAll tools passed.\n")

    return len(failed) == 0


if __name__ == "__main__":
    results, generated = asyncio.run(run_tests())
    all_passed = print_report(results, generated)
    sys.exit(0 if all_passed else 1)
