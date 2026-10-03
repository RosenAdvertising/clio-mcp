#!/usr/bin/env python3
"""Offline OpenAPI drift checks for clio-mcp.

This script does not read credentials and does not call Clio. It compares the
local endpoint registry against a supplied Clio OpenAPI v4 JSON document.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mcp.types import LATEST_PROTOCOL_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS_PATH = REPO_ROOT / "clio_mcp" / "endpoints.yaml"
EXPECTED_MCP_PROTOCOL_VERSION = "2026-07-28"

RESOURCE_SCHEMAS = {
    "activities": "Activity",
    "bills": "Bill",
    "calendar_entries": "CalendarEntry",
    "communications": "Communication",
    "contacts": "Contact",
    "documents": "Document",
    "matters": "Matter",
    "notes": "Note",
    "practice_areas": "PracticeArea",
    "relationships": "Relationship",
    "tasks": "Task",
    "timer": "Timer",
    "users": "User",
}

# Current client/server body construction. Keep this explicit so failures are
# actionable instead of depending on fragile Python AST inference.
BODY_FIELDS = {
    "create_matter": {
        "client",
        "description",
        "status",
        "practice_area",
        "responsible_attorney",
    },
    "update_matter": {
        "description",
        "status",
        "responsible_attorney",
        "practice_area",
        "close_date",
    },
    "create_contact": {
        "type",
        "first_name",
        "last_name",
        "name",
        "email_addresses",
        "phone_numbers",
    },
    "create_task": {"name", "description", "due_at", "priority", "matter", "assignee"},
    "update_task": {"name", "description", "due_at", "priority", "status", "assignee"},
    "create_calendar_entry": {
        "summary",
        "start_at",
        "end_at",
        "calendar_owner",
        "matter",
        "description",
        "location",
        "attendees",
    },
    "create_time_entry": {
        "type",
        "date",
        "quantity",
        "matter",
        "note",
        "user",
        "price",
    },
    "create_expense_entry": {
        "type",
        "date",
        "quantity",
        "matter",
        "note",
        "price",
        "non_billable",
    },
    "upload_document_create": {"name", "fully_uploaded", "parent"},
    "upload_document_finalize": {"fully_uploaded"},
    "create_note": {"subject", "detail", "type", "matter", "contact"},
    "add_relationship": {"matter", "contact", "description"},
    "start_timer": {"activity"},
}

# Fields that the current client always sends when the tool is called with its
# required MCP args. Optional/conditional fields stay in BODY_FIELDS only.
ALWAYS_BODY_FIELDS = {
    "create_matter": {"client", "description", "status"},
    "create_contact": {"type", "name"},
    "create_task": {"name", "description", "priority", "assignee"},
    "create_calendar_entry": {"summary", "start_at", "end_at", "calendar_owner"},
    "create_time_entry": {"type", "date", "quantity", "matter"},
    "create_expense_entry": {
        "type",
        "date",
        "quantity",
        "matter",
        "note",
        "price",
        "non_billable",
    },
    # parent always required by Clio (matter or folder); fully_uploaded goes in finalize step only
    "upload_document_create": {"name", "parent"},
    "upload_document_finalize": {"fully_uploaded"},
    # OpenAPI incorrectly marks both matter+contact required; client always sends exactly one
    "create_note": {"subject", "detail", "type", "matter", "contact"},
    "add_relationship": {"matter", "contact", "description"},
    "start_timer": {"activity"},
}

PARAM_ALIASES = {
    "list_calendar_entries": {"start_date": "from", "end_date": "to"},
    "list_activities": {"activity_type": "type"},
    "list_bills": {
        "status": "state",
        "overdue": "overdue_only",
        "start_date": "issued_after",
        "end_date": "issued_before",
    },
}

LOCAL_ONLY_PARAMS = {
    "download_document": {"output_path"},
}

KNOWN_DEFAULTS = {
    ("create_matter", "status"): "open",
    ("list_matters", "status"): "open",
    ("create_contact", "type"): "Person",
    ("create_task", "priority"): "Normal",
    ("list_tasks", "status"): "pending",
    ("create_time_entry", "type"): "TimeEntry",
    ("create_expense_entry", "type"): "ExpenseEntry",
    ("create_note", "type"): "Matter",
}

LIST_SAFETY_PARAMS = {
    "list_matters": {"order"},
    "list_contacts": {"order"},
    "list_tasks": {"order"},
    "list_bills": {
        "order",
        "issued_after",
        "issued_before",
        "created_since",
        "updated_since",
    },
    "list_activities": {
        "order",
        "start_date",
        "end_date",
        "created_since",
        "updated_since",
    },
    "list_documents": {"order", "created_since", "updated_since"},
    "list_notes": {"order", "created_since", "updated_since"},
    "list_communications": {
        "order",
        "received_since",
        "received_before",
        "created_since",
        "updated_since",
    },
}


@dataclass
class Finding:
    level: str
    tool: str
    message: str
    detail: str = ""


def check_mcp_revision() -> list[Finding]:
    """Fail when the installed SDK no longer targets the migrated MCP revision."""
    if LATEST_PROTOCOL_VERSION == EXPECTED_MCP_PROTOCOL_VERSION:
        return []
    return [
        Finding(
            "ERROR",
            "mcp_protocol",
            "installed MCP SDK targets the wrong protocol revision",
            (
                f"expected {EXPECTED_MCP_PROTOCOL_VERSION!r}, "
                f"got {LATEST_PROTOCOL_VERSION!r}"
            ),
        )
    ]


def load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f)


def openapi_path(endpoint_path: str) -> str:
    path = endpoint_path.removeprefix("/api/v4")
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith(".json"):
        path += ".json"
    return path


def query_params(operation: dict[str, Any]) -> set[str]:
    return {
        p["name"]
        for p in operation.get("parameters", [])
        if p.get("in") == "query" and p.get("name")
    }


def request_data_schema(operation: dict[str, Any]) -> dict[str, Any]:
    return (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("properties", {})
        .get("data", {})
    )


def schema_props(
    openapi: dict[str, Any], schema_name: str, seen: set[str] | None = None
) -> set[str]:
    if seen is None:
        seen = set()
    if schema_name in seen:
        return set()
    seen.add(schema_name)

    schemas = openapi.get("components", {}).get("schemas", {})
    schema = schemas.get(schema_name, {})
    props = set(schema.get("properties", {}).keys())
    for item in schema.get("allOf", []):
        ref = item.get("$ref")
        if ref:
            props |= schema_props(openapi, ref.rsplit("/", 1)[-1], seen)
        else:
            props |= set(item.get("properties", {}).keys())
    return props


def path_resource(endpoint_path: str) -> str:
    return endpoint_path.removeprefix("/api/v4/").split("/", 1)[0]


def requested_field_roots(tool: dict[str, Any]) -> set[str]:
    roots = set()
    for field in tool.get("requested_fields", []):
        roots.add(str(field).split("{", 1)[0])
    return roots


def enum_for_body_field(operation: dict[str, Any], field: str) -> set[str]:
    field_schema = request_data_schema(operation).get("properties", {}).get(field, {})
    return set(field_schema.get("enum", []))


def check(openapi: dict[str, Any], endpoints: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    paths = openapi.get("paths", {})

    for tool in endpoints.get("tools", []):
        name = tool["name"]
        method = tool["method"].lower()
        spec_path = openapi_path(tool["path"])
        path_item = paths.get(spec_path)

        if path_item is None:
            findings.append(
                Finding(
                    "ERROR",
                    name,
                    "path not found in OpenAPI",
                    f"{tool['method']} {tool['path']} -> {spec_path}",
                )
            )
            continue

        operation = path_item.get(method)
        if operation is None:
            findings.append(
                Finding(
                    "ERROR",
                    name,
                    "method not found for OpenAPI path",
                    f"{tool['method']} {spec_path}",
                )
            )
            continue

        qp = query_params(operation)
        declared_params = set(tool.get("optional_params", [])) | set(
            tool.get("required_params", [])
        )
        aliases = PARAM_ALIASES.get(name, {})
        local_only = LOCAL_ONLY_PARAMS.get(name, set())
        path_param_names = set(
            part.strip("{}").removesuffix("_id")
            for part in tool["path"].split("/")
            if part.startswith("{")
        )
        endpoint_id_names = {
            "id",
            "matter_id",
            "contact_id",
            "task_id",
            "document_id",
            "trust_account_id",
            "timer_id",
        }
        if method == "get":
            for param in sorted(declared_params):
                if (
                    param in endpoint_id_names
                    or param in path_param_names
                    or param in local_only
                ):
                    continue
                api_param = aliases.get(param, param)
                if api_param not in qp:
                    findings.append(
                        Finding(
                            "ERROR",
                            name,
                            "declared GET param not in OpenAPI",
                            f"{param} -> {api_param}",
                        )
                    )

            useful = LIST_SAFETY_PARAMS.get(name, set()) & qp
            exposed = set(tool.get("optional_params", []))
            missing_useful = useful - exposed
            if missing_useful:
                findings.append(
                    Finding(
                        "WARN",
                        name,
                        "useful list safety params not exposed",
                        ", ".join(sorted(missing_useful)),
                    )
                )

        if method in {"post", "patch"}:
            data_schema = request_data_schema(operation)
            required = set(data_schema.get("required", []))
            body_fields = BODY_FIELDS.get(name)
            always_fields = ALWAYS_BODY_FIELDS.get(name, set())
            if required and body_fields is not None:
                missing = required - always_fields
                if missing:
                    findings.append(
                        Finding(
                            "ERROR",
                            name,
                            "required body fields missing from client payload",
                            ", ".join(sorted(missing)),
                        )
                    )

            allowed_body = set(data_schema.get("properties", {}).keys())
            if body_fields and allowed_body:
                extra = body_fields - allowed_body
                if extra:
                    findings.append(
                        Finding(
                            "WARN",
                            name,
                            "client payload fields not documented in OpenAPI body",
                            ", ".join(sorted(extra)),
                        )
                    )

        resource = path_resource(tool["path"])
        schema_name = RESOURCE_SCHEMAS.get(resource)
        if schema_name:
            props = schema_props(openapi, schema_name)
            unknown = requested_field_roots(tool) - props
            if unknown:
                findings.append(
                    Finding(
                        "ERROR",
                        name,
                        "requested field root not found in OpenAPI schema",
                        ", ".join(sorted(unknown)),
                    )
                )

        default_key = (name, "status") if (name, "status") in KNOWN_DEFAULTS else None
        if default_key and method in {"get", "post", "patch"}:
            enum = set()
            if method == "get":
                for param in operation.get("parameters", []):
                    if param.get("name") == "status":
                        enum = set(param.get("schema", {}).get("enum", []))
                        break
            else:
                enum = enum_for_body_field(operation, "status")
            value = KNOWN_DEFAULTS[default_key]
            if enum and value not in enum:
                findings.append(
                    Finding(
                        "ERROR",
                        name,
                        "default status not in OpenAPI enum",
                        f"{value!r} not in {sorted(enum)}",
                    )
                )

        for field in ("type", "priority"):
            key = (name, field)
            if key not in KNOWN_DEFAULTS or method not in {"post", "patch"}:
                continue
            enum = enum_for_body_field(operation, field)
            value = KNOWN_DEFAULTS[key]
            if enum and value not in enum:
                findings.append(
                    Finding(
                        "ERROR",
                        name,
                        f"default {field} not in OpenAPI enum",
                        f"{value!r} not in {sorted(enum)}",
                    )
                )

    return findings


def print_report(findings: list[Finding]) -> None:
    errors = [f for f in findings if f.level == "ERROR"]
    warnings = [f for f in findings if f.level == "WARN"]
    status = "FAIL" if errors else "PASS"

    print(f"Spec check: {status}")
    print(f"Errors: {len(errors)}")
    print(f"Warnings: {len(warnings)}")
    print()

    for finding in findings:
        print(f"[{finding.level}] {finding.tool}: {finding.message}")
        if finding.detail:
            print(f"  {finding.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare clio-mcp endpoint metadata to Clio OpenAPI JSON."
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--openapi", type=Path, help="Path to clio-openapi-v4.json")
    inputs.add_argument(
        "--mcp-only",
        action="store_true",
        help="Check only the installed MCP protocol revision",
    )
    args = parser.parse_args()

    protocol_findings = check_mcp_revision()
    if args.mcp_only:
        print_report(protocol_findings)
        return 1 if protocol_findings else 0

    assert args.openapi is not None
    if not args.openapi.is_file():
        print(f"OpenAPI file not found: {args.openapi}", file=sys.stderr)
        return 2

    try:
        openapi = load_json(args.openapi)
        endpoints = load_yaml(ENDPOINTS_PATH)
    except Exception as exc:
        print(f"Failed to load spec inputs: {exc}", file=sys.stderr)
        return 2

    findings = protocol_findings + check(openapi, endpoints)
    print_report(findings)
    return 1 if any(f.level == "ERROR" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
