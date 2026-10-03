from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.fernet import Fernet

from clio_mcp import credentials, server
from clio_mcp import client as clio_client
from clio_mcp.client import ClioClient, MAX_LIST_PAGES, _fields


class FakeResponse:
    def __init__(self, body: Any, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self) -> Any:
        return self._body


class RecordingClient(ClioClient):
    def __init__(self, responses: list[Any]):
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def _request(
        self, method, path, params=None, json_body=None, retry=True
    ) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_body": json_body,
            }
        )
        return FakeResponse(next(self.responses))


def page(data: list[dict[str, Any]], next_url: str | None = None) -> dict[str, Any]:
    return {"data": data, "meta": {"paging": {"next": next_url}}}


def test_create_time_entry_posts_hourly_rate_without_multiplying(monkeypatch) -> None:
    client = RecordingClient([{"data": {"id": 9}}])
    monkeypatch.setattr(server, "ClioClient", lambda: client)

    result = json.loads(
        server.create_time_entry(
            matter_id=1,
            date="2026-07-21",
            hours=2.0,
            rate=200.0,
        )
    )

    assert result == {"data": {"id": 9}}
    assert client.requests == [
        {
            "method": "POST",
            "path": "activities",
            "params": {"fields": _fields("create_time_entry")},
            "json_body": {
                "data": {
                    "type": "TimeEntry",
                    "date": "2026-07-21",
                    "quantity": 7200,
                    "matter": {"id": 1},
                    "price": 200.0,
                }
            },
        }
    ]


def test_billing_rate_regression_uses_each_acting_users_authorization(
    monkeypatch, tmp_path
) -> None:
    for name, filename in (
        ("TOKEN_FILE", "tokens.json"),
        ("CREDENTIAL_STATE_FILE", "credential_state.json"),
    ):
        monkeypatch.setattr(credentials, name, tmp_path / filename)
    monkeypatch.setenv("CLIO_MCP_USE_KEYRING", "0")
    monkeypatch.setenv("CLIO_MCP_TOKEN_KEY", Fernet.generate_key().decode())
    credentials.set_token_cache("local-a", "a@example.com", {"access_token": "token-a"})
    credentials.set_token_cache("local-b", "b@example.com", {"access_token": "token-b"})
    calls: list[tuple[str, float]] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        @staticmethod
        def json():
            return {"data": {"id": 9}}

    first = ClioClient("local-a")
    second = ClioClient("local-b")

    def record(client: ClioClient):
        def request(*args, **kwargs):
            calls.append(
                (
                    client.session.headers["Authorization"],
                    kwargs["json"]["data"]["price"],
                )
            )
            return Response()

        return request

    monkeypatch.setattr(first.session, "request", record(first))
    monkeypatch.setattr(second.session, "request", record(second))
    first.create_time_entry("2026-07-21", 7200, 1, price=200.0)
    second.create_time_entry("2026-07-21", 7200, 1, price=200.0)
    assert calls == [("Bearer token-a", 200.0), ("Bearer token-b", 200.0)]


@pytest.mark.parametrize(("hours", "quantity_seconds"), [(2.01, 7236), (0.25, 900)])
def test_create_time_entry_rounds_hours_to_exact_seconds(
    monkeypatch, hours: float, quantity_seconds: int
) -> None:
    client = RecordingClient([{"data": {"id": 9}}])
    monkeypatch.setattr(server, "ClioClient", lambda: client)

    server.create_time_entry(
        matter_id=1,
        date="2026-07-21",
        hours=hours,
        rate=200.0,
    )

    assert client.requests == [
        {
            "method": "POST",
            "path": "activities",
            "params": {"fields": _fields("create_time_entry")},
            "json_body": {
                "data": {
                    "type": "TimeEntry",
                    "date": "2026-07-21",
                    "quantity": quantity_seconds,
                    "matter": {"id": 1},
                    "price": 200.0,
                }
            },
        }
    ]


@pytest.mark.parametrize(
    "hours",
    [-1.0, float("nan"), 1e308],
    ids=["negative", "nan", "overflows-seconds"],
)
def test_create_time_entry_rejects_nonpositive_or_nonfinite_hours(
    monkeypatch, hours: float
) -> None:
    monkeypatch.setattr(
        server,
        "ClioClient",
        lambda: pytest.fail("invalid hours must not construct a Clio client"),
    )

    with pytest.raises(ValueError, match="hours must be finite and greater than 0"):
        server.create_time_entry(matter_id=1, date="2026-07-21", hours=hours)


def test_create_expense_entry_posts_per_unit_price_without_multiplying() -> None:
    client = RecordingClient([{"data": {"id": 10}}])

    client.create_expense_entry(
        matter_id=1,
        amount_dollars=125.0,
        description="Three filing copies",
        date="2026-07-21",
        quantity=3,
    )

    posted_data = client.requests[0]["json_body"]["data"]
    assert posted_data["quantity"] == 3
    assert posted_data["price"] == 125.0


def test_notes_emit_lowercase_type_in_queries() -> None:
    client = RecordingClient([page([]), page([])])

    client.list_notes(matter_id=1)
    client.list_notes(contact_id=2)

    assert client.requests[0]["params"]["type"] == "matter"
    assert client.requests[1]["params"]["type"] == "contact"


def test_create_note_matter_association_sets_matter_type() -> None:
    client = RecordingClient([{"data": {"id": 11}}])

    client.create_note(
        subject="Call",
        detail="Client follow-up",
        matter_id=1,
    )

    assert client.requests == [
        {
            "method": "POST",
            "path": "notes",
            "params": {"fields": _fields("create_note")},
            "json_body": {
                "data": {
                    "subject": "Call",
                    "detail": "Client follow-up",
                    "type": "matter",
                    "matter": {"id": 1},
                }
            },
        }
    ]


def test_create_note_tool_exposes_and_forwards_contact_id(monkeypatch) -> None:
    client = RecordingClient([{"data": {"id": 12}}])
    monkeypatch.setattr(server, "ClioClient", lambda: client)

    server.create_note(subject="Call", detail="Client follow-up", contact_id=2)

    body = client.requests[0]["json_body"]["data"]
    assert body["type"] == "contact"
    assert body["contact"] == {"id": 2}
    assert "matter" not in body


def test_create_note_contact_association_sets_contact_type() -> None:
    client = RecordingClient([{"data": {"id": 12}}])

    client.create_note(
        subject="Call",
        detail="Client follow-up",
        contact_id=2,
    )

    assert client.requests == [
        {
            "method": "POST",
            "path": "notes",
            "params": {"fields": _fields("create_note")},
            "json_body": {
                "data": {
                    "subject": "Call",
                    "detail": "Client follow-up",
                    "type": "contact",
                    "contact": {"id": 2},
                }
            },
        }
    ]


@pytest.mark.parametrize(
    "associations",
    [{}, {"matter_id": 1, "contact_id": 2}],
    ids=["neither", "both"],
)
def test_create_note_requires_exactly_one_association(associations) -> None:
    client = RecordingClient([])

    with pytest.raises(ValueError, match="Exactly one of matter_id or contact_id"):
        client.create_note(subject="Call", detail="Client follow-up", **associations)


def test_create_note_rejects_type_conflicting_with_association() -> None:
    client = RecordingClient([])

    with pytest.raises(ValueError, match="conflicts with the contact association"):
        client.create_note(
            subject="Call",
            detail="Client follow-up",
            note_type="Matter",
            contact_id=2,
        )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RecordingSession:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.request_times: list[float] = []

    def request(self, *args, **kwargs) -> FakeResponse:
        self.request_times.append(self.clock())
        return FakeResponse({"data": {}})


def request_client(
    session: RecordingSession, sub: str = "local-rate-user"
) -> ClioClient:
    client = ClioClient.__new__(ClioClient)
    client_with_fake: Any = client
    client_with_fake.session = session
    client_with_fake.user_sub = sub
    client_with_fake.tm = type("TokenManagerStub", (), {"generation": 0})()
    return client


def test_per_sub_rate_limiter_caps_sixty_request_burst_at_fifty(monkeypatch) -> None:
    clock = FakeClock()
    session = RecordingSession(clock)
    client = request_client(session)
    limiter = clio_client.RollingWindowRateLimiter(
        50, 60.0, clock=clock, sleeper=clock.sleep
    )
    monkeypatch.setattr(clio_client, "_rate_limiter_for", lambda sub: limiter)
    monkeypatch.setattr(
        clio_client.credentials, "credential_is_dead", lambda sub: False
    )

    for _ in range(60):
        client._request("GET", "users/who_am_i")

    assert session.request_times[:50] == [0.0] * 50
    assert session.request_times[50:] == [60.0] * 10


def test_rate_limiter_is_shared_across_same_sub_client_instances(monkeypatch) -> None:
    clock = FakeClock()
    session = RecordingSession(clock)
    first_client = request_client(session)
    second_client = request_client(session)
    limiter = clio_client.RollingWindowRateLimiter(
        2, 60.0, clock=clock, sleeper=clock.sleep
    )
    monkeypatch.setattr(clio_client, "_rate_limiter_for", lambda sub: limiter)
    monkeypatch.setattr(
        clio_client.credentials, "credential_is_dead", lambda sub: False
    )

    first_client._request("GET", "users/who_am_i")
    second_client._request("GET", "users/who_am_i")
    first_client._request("GET", "users/who_am_i")

    assert session.request_times == [0.0, 0.0, 60.0]


def test_list_bills_uses_documented_overdue_filter_not_invalid_state() -> None:
    client = RecordingClient([page([]), page([]), page([])])

    client.list_bills(status="overdue")
    client.list_bills(overdue=True)
    client.list_bills(status="paid")

    compatibility_params = client.requests[0]["params"]
    explicit_params = client.requests[1]["params"]
    state_params = client.requests[2]["params"]
    assert compatibility_params["overdue_only"] is True
    assert "state" not in compatibility_params
    assert explicit_params["overdue_only"] is True
    assert "state" not in explicit_params
    assert state_params["state"] == "paid"
    assert "overdue_only" not in state_params


def test_calendar_bare_dates_emit_inclusive_day_bounds() -> None:
    client = RecordingClient([page([])])

    client.list_calendar_entries(start_date="2026-07-01", end_date="2026-07-21")

    params = client.requests[0]["params"]
    assert params["from"] == "2026-07-01T00:00:00Z"
    assert params["to"] == "2026-07-21T23:59:59Z"


def test_list_pagination_follows_three_pages_without_duplicates() -> None:
    client = RecordingClient(
        [
            page(
                [{"id": 1}, {"id": 2}],
                "https://app.clio.com/api/v4/matters.json?page_token=page-2",
            ),
            page(
                [{"id": 3}],
                "https://app.clio.com/api/v4/matters.json?page_token=page-3",
            ),
            page([{"id": 4}, {"id": 5}]),
        ]
    )

    result = client.list_matters()

    assert [record["id"] for record in result["data"]] == [1, 2, 3, 4, 5]
    assert len(client.requests) == 3
    assert "truncated" not in result


def test_list_pagination_honors_five_page_cap_and_marks_truncation() -> None:
    responses = [
        page(
            [{"id": page_number}],
            f"https://app.clio.com/api/v4/contacts.json?page_token=page-{page_number + 1}",
        )
        for page_number in range(1, MAX_LIST_PAGES + 1)
    ]
    client = RecordingClient(responses)

    result = client.list_contacts()

    assert [record["id"] for record in result["data"]] == [1, 2, 3, 4, 5]
    assert len(client.requests) == MAX_LIST_PAGES
    assert result["truncated"] is True
    assert "narrow" in result["note"]


def test_every_paginated_list_method_uses_bounded_helper() -> None:
    client = object.__new__(ClioClient)
    calls: list[str] = []

    def fake_get_list(path, fields=None, **params):
        calls.append(path)
        return {"data": []}

    client_with_fake: Any = client
    client_with_fake._get_list = fake_get_list
    client.list_matters()
    client.list_contacts()
    client.list_tasks()
    client.list_calendar_entries()
    client.list_activities()
    client.list_bills()
    client.list_documents()
    client.list_notes()
    client.list_practice_areas()
    client.list_users()
    client.list_communications()

    assert calls == [
        "matters",
        "contacts",
        "tasks",
        "calendar_entries",
        "activities",
        "bills",
        "documents",
        "notes",
        "practice_areas",
        "users",
        "communications",
    ]
