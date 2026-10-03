"""Regression policy for keeping identity values out of product logs."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from clio_mcp import client as clio_client
from clio_mcp import credentials
from clio_mcp.setup import verify


ROOT = Path(__file__).resolve().parents[1]
LOG_METHODS = {
    "critical",
    "debug",
    "error",
    "exception",
    "info",
    "log",
    "warn",
    "warning",
}
RAW_IDENTITY_REFERENCES = {
    "email",
    "expected_clio_email",
    "first_name",
    "full_name",
    "label",
    "last_name",
    "name",
    "sub",
}
IDENTITY_LOG_EVENTS = {"clio_credential_dead": 1}


def _logging_calls() -> Iterator[tuple[Path, str, ast.Call]]:
    for path in sorted((ROOT / "clio_mcp").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in LOG_METHODS:
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id not in {
                "log",
                "logger",
                "logging",
            }:
                continue
            yield path, source, node


def _event(call: ast.Call) -> str | None:
    extra = next((kw.value for kw in call.keywords if kw.arg == "extra"), None)
    if not isinstance(extra, ast.Dict):
        return None
    for key, value in zip(extra.keys, extra.values, strict=True):
        if (
            isinstance(key, ast.Constant)
            and key.value == "event"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _raw_identity_references(call: ast.Call) -> set[str]:
    references = {node.id for node in ast.walk(call) if isinstance(node, ast.Name)}
    references.update(
        node.attr for node in ast.walk(call) if isinstance(node, ast.Attribute)
    )
    references.update(
        node.value
        for node in ast.walk(call)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return references & RAW_IDENTITY_REFERENCES


def test_all_product_logging_calls_exclude_raw_identity_references() -> None:
    violations = []
    for path, _source, call in _logging_calls():
        if raw_references := _raw_identity_references(call):
            violations.append(
                f"{path.relative_to(ROOT)}:{call.lineno}: "
                f"{', '.join(sorted(raw_references))}"
            )
    assert violations == []


@pytest.mark.parametrize(("event", "expected_count"), IDENTITY_LOG_EVENTS.items())
def test_fixed_identity_log_sites_remain_pii_free(
    event: str, expected_count: int
) -> None:
    calls = [
        (path, source, call)
        for path, source, call in _logging_calls()
        if _event(call) == event
    ]
    assert len(calls) == expected_count
    for path, source, call in calls:
        assert _raw_identity_references(call) == set(), (
            f"{path.relative_to(ROOT)}:{call.lineno}: "
            f"{ast.get_source_segment(source, call)}"
        )


def test_verification_cli_does_not_print_clio_name_or_email(
    monkeypatch, capsys
) -> None:
    private_name = "Private Person"
    private_email = "private.person@example.test"

    class FakeClient:
        def __init__(self, subject: str):
            assert subject == credentials.LOCAL_SUBJECT

        @staticmethod
        def who_am_i() -> dict[str, str]:
            return {"name": private_name, "email": private_email}

        @staticmethod
        def list_matters(*, limit: int) -> dict[str, list[object]]:
            assert limit == 5
            return {"data": []}

    monkeypatch.setattr(clio_client, "ClioClient", FakeClient)

    assert verify.check_api() is True
    output = capsys.readouterr().out
    assert "Authenticated successfully" in output
    assert private_name not in output
    assert private_email not in output
