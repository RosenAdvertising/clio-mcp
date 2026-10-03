#!/usr/bin/env python3
"""Clio Manage API v4 client. Handles OAuth tokens, auto-refresh, and all API operations."""

import contextvars
import logging
import math
import os
import threading
import time
import yaml
from collections import deque
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from clio_mcp import credentials, files
from clio_mcp.transfers import transfer, TransferError

BASE_URL = "https://app.clio.com/api/v4"
TOKEN_URL = "https://app.clio.com/oauth/token"
CONFIG_DIR = Path.home() / ".clio-mcp"

credentials.load_into_environ(["CLIO_CLIENT_ID", "CLIO_CLIENT_SECRET"])

# Optional callers can bound a sequence of Clio requests with one monotonic
# deadline. The synchronous request path clamps its own deadlines to it.
_CALL_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "clio_call_deadline", default=None
)
SLOW_OR_RATE_LIMITED_MESSAGE = "Clio is slow or rate limiting, retry shortly"
# Below this much remaining budget an HTTP call cannot be bounded honestly: the
# smallest workable connect and read allowances would together outlast the
# deadline, so the call is refused instead of started.
MIN_HTTP_BUDGET_SECONDS = 0.002
BUDGET_RETRY_AFTER_SECONDS = 5


def set_call_deadline(deadline: float | None) -> None:
    """Bound every Clio request made by this call to one monotonic deadline."""
    _CALL_DEADLINE.set(deadline)


def call_deadline_remaining() -> float:
    """Seconds left in the current call budget; inf when no budget applies."""
    deadline = _CALL_DEADLINE.get()
    if deadline is None:
        return math.inf
    return deadline - time.monotonic()


def phase_timeouts(remaining: float) -> tuple[float, float]:
    """Split `remaining` so connecting plus reading cannot outlast the deadline.

    requests applies a (connect, read) tuple once per phase rather than as a
    total, so passing the same `remaining` to both would let a slow connect
    followed by a slow read run for twice the budget. Connect may claim at most
    half of what is left and read gets the rest, each still capped at its
    normal ceiling, so the two together are bounded by `remaining`.
    """
    if remaining == math.inf:
        return float(HTTP_CONNECT_TIMEOUT_SECONDS), float(HTTP_READ_TIMEOUT_SECONDS)
    budget = max(MIN_HTTP_BUDGET_SECONDS, remaining)
    connect = min(HTTP_CONNECT_TIMEOUT_SECONDS, max(0.001, budget / 2))
    read = min(HTTP_READ_TIMEOUT_SECONDS, max(0.001, budget - connect))
    return connect, read


def timeout_was_budget_bound(timeout: tuple[float, float], remaining: float) -> bool:
    """True when the call budget, not the default ceilings, set these timeouts.

    A phase timeout then means the budget is spent, even though the unused half
    of the split leaves a little wall-clock time on the deadline.
    """
    return remaining != math.inf and sum(timeout) >= remaining - 1e-6


CLIENT_ID = os.environ.get("CLIO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIO_CLIENT_SECRET", "")
ACCESS_TOKEN_LIFETIME = timedelta(days=30)
HTTP_CONNECT_TIMEOUT_SECONDS = 10
HTTP_READ_TIMEOUT_SECONDS = 30
API_REQUEST_DEADLINE_SECONDS = 90
MAX_RATE_LIMIT_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 30
DEFAULT_REQUESTS_PER_MINUTE = 50
REQUEST_RATE_WINDOW_SECONDS = 60.0
MAX_LIST_PAGES = 5
LIST_TRUNCATION_NOTE = (
    "More records are available; narrow the request with filters, a shorter date "
    "range, or a more specific query."
)
logger = logging.getLogger(__name__)


def _requests_per_minute() -> int:
    raw_limit = os.environ.get(
        "CLIO_REQUESTS_PER_MINUTE", str(DEFAULT_REQUESTS_PER_MINUTE)
    )
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = DEFAULT_REQUESTS_PER_MINUTE
    if limit <= 0:
        limit = DEFAULT_REQUESTS_PER_MINUTE
    return limit


class RollingWindowRateLimiter:
    """Thread-safe admission control shared by one subject's client instances."""

    def __init__(self, limit, window_seconds, *, clock=None, sleeper=None):
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, max_wait_seconds):
        deadline = self._clock() + max(0.0, max_wait_seconds)
        while True:
            with self._lock:
                now = self._clock()
                window_start = now - self.window_seconds
                while self._timestamps and self._timestamps[0] <= window_start:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.limit:
                    self._timestamps.append(now)
                    return True

                wait_seconds = self._timestamps[0] + self.window_seconds - now
                if wait_seconds >= deadline - now:
                    return False
            self._sleep(wait_seconds)

    def next_slot_seconds(self) -> float:
        """Seconds until the next outbound slot frees, without admitting."""
        with self._lock:
            now = self._clock()
            window_start = now - self.window_seconds
            while self._timestamps and self._timestamps[0] <= window_start:
                self._timestamps.popleft()
            if len(self._timestamps) < self.limit:
                return 0.0
            return self._timestamps[0] + self.window_seconds - now


_RATE_LIMITERS_LOCK = threading.Lock()
_RATE_LIMITERS: dict[str, RollingWindowRateLimiter] = {}


def _rate_limiter_for(sub: str) -> RollingWindowRateLimiter:
    """Return the shared outbound quota bucket for one local credential subject."""
    with _RATE_LIMITERS_LOCK:
        return _RATE_LIMITERS.setdefault(
            sub,
            RollingWindowRateLimiter(
                _requests_per_minute(), REQUEST_RATE_WINDOW_SECONDS
            ),
        )


def reset_rate_limiter(sub: str) -> None:
    """Forget one local credential record's rate-limit bucket."""
    with _RATE_LIMITERS_LOCK:
        _RATE_LIMITERS.pop(sub, None)


class CredentialDeadError(RuntimeError):
    """Raised when Clio has permanently rejected the stored credential."""


class TokenRefreshError(RuntimeError):
    """Raised for a retryable or otherwise non-terminal refresh failure."""


class ClioDegradedError(RuntimeError):
    """Raised for a retryable upstream API condition without killing a grant."""


class ClioRateLimitedError(ClioDegradedError):
    """Raised when a request cannot wait for a rate-limit slot."""

    code = "clio_rate_limited"

    def __init__(self, retry_after: float):
        super().__init__(SLOW_OR_RATE_LIMITED_MESSAGE)
        self.retry_after = max(0.0, float(retry_after))


class ClioCallBudgetExceededError(ClioRateLimitedError):
    """Raised when the per-call time budget ran out mid-request."""

    code = "clio_call_budget_exceeded"

    def __init__(self, retry_after: float = BUDGET_RETRY_AFTER_SECONDS):
        super().__init__(retry_after)


class ClioConnectionMissingError(RuntimeError):
    """Raised before networking when the acting user has no stored grant."""


class CredentialChangedError(RuntimeError):
    """Raised when credentials rotate during an admitted request."""


CREDENTIAL_CHANGED_MESSAGE = "Clio credentials changed during the request"


def _ensure_current_authorization(sub: str, expected_generation: int) -> None:
    """Fail an admitted request if credential rotation won the race."""
    if credentials.credential_generation(sub) != expected_generation:
        raise CredentialChangedError(CREDENTIAL_CHANGED_MESSAGE)


def _acting_user_sub() -> str:
    """Use the reserved local credential record for stdio."""
    return credentials.LOCAL_SUBJECT


def stamp_token_payload(
    tokens: dict[str, Any],
    *,
    previous_refresh_token: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add deterministic lifetime metadata and preserve an unrotated grant."""
    stamped = {
        key: tokens[key]
        for key in ("access_token", "refresh_token", "token_type", "expires_in")
        if key in tokens
    }
    if previous_refresh_token and not stamped.get("refresh_token"):
        stamped["refresh_token"] = previous_refresh_token

    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    stamped["refreshed_at"] = timestamp.isoformat()
    expires_in = stamped.get("expires_in")
    try:
        lifetime_seconds = (
            float(expires_in)
            if expires_in is not None
            else ACCESS_TOKEN_LIFETIME.total_seconds()
        )
    except (TypeError, ValueError):
        lifetime_seconds = ACCESS_TOKEN_LIFETIME.total_seconds()
    stamped["expires_at"] = timestamp.timestamp() + lifetime_seconds
    return stamped


def _load_endpoints():
    yaml_path = Path(__file__).parent / "endpoints.yaml"
    with open(yaml_path) as f:
        spec = yaml.safe_load(f)
    return {
        t["name"]: ",".join(str(f) for f in t.get("requested_fields", []))
        for t in spec.get("tools", [])
    }


_ENDPOINT_FIELDS = _load_endpoints()


def _fields(tool_name: str) -> str:
    return _ENDPOINT_FIELDS[tool_name]


class TokenManager:
    def __init__(self, user_sub: str):
        self.user_sub = user_sub
        self.tokens, self.generation = credentials.credential_snapshot(user_sub)
        self.last_refresh_adopted_external_generation = False

    def _load(self):
        """Load token cache via the pluggable credential store.

        Resolution order: OS keyring -> legacy tokens.json file (migration
        fallback).  Returns an empty dict if no tokens are found.
        """
        cached, generation = credentials.credential_snapshot(self.user_sub)
        self.generation = generation
        return cached

    def save(self, tokens, *, email: str | None = None):
        """Persist token cache via the pluggable credential store.

        When keyring is active the tokens are written there and any legacy
        tokens.json plaintext file is removed (migration).  Falls back to
        a 0600 tokens.json file when keyring is unavailable/disabled.
        """
        resolved_email = email or credentials.credential_email(self.user_sub)
        if not resolved_email:
            raise TokenRefreshError("Cannot save tokens without a credential email")
        credentials.set_token_cache(self.user_sub, resolved_email, tokens)
        self.tokens, self.generation = credentials.credential_snapshot(self.user_sub)

    @property
    def access_token(self):
        return self.tokens.get("access_token", "")

    @property
    def refresh_token(self):
        return self.tokens.get("refresh_token", "")

    def refresh(self):
        self.last_refresh_adopted_external_generation = False
        with credentials.credential_transaction(self.user_sub):
            latest, latest_generation = credentials.credential_snapshot(self.user_sub)
            if latest_generation != self.generation:
                self.tokens = latest
                self.generation = latest_generation
                self.last_refresh_adopted_external_generation = True
                return latest
            if not self.refresh_token:
                raise TokenRefreshError("No refresh token is stored")

            app_credentials = credentials.resolve_app_credentials()
            if app_credentials is None:
                raise TokenRefreshError("No Clio application credential is configured")
            # Token refreshes draw from the same per-token quota as API calls.
            # A refresh holds this subject's refresh lock, so the wait must never
            # outlive the caller's own budget: otherwise one slow refresh pins
            # every concurrent request for the same subject.
            refresh_wait = max(0.0, min(30.0, call_deadline_remaining()))
            if not _rate_limiter_for(self.user_sub).acquire(
                max_wait_seconds=refresh_wait
            ):
                raise TokenRefreshError(
                    "Clio request rate limit wait exceeded while refreshing the token"
                )
            _ensure_current_authorization(self.user_sub, self.generation)
            # The refresh runs inside this subject's credential transaction, so
            # it must never outlive the caller's budget: otherwise the response
            # is already sent while the thread still holds the user's lock.
            remaining = call_deadline_remaining()
            if remaining <= MIN_HTTP_BUDGET_SECONDS:
                raise ClioCallBudgetExceededError()
            refresh_timeout = phase_timeouts(remaining)
            try:
                resp = requests.post(
                    TOKEN_URL,
                    data={
                        "client_id": app_credentials.client_id,
                        "client_secret": app_credentials.client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                    },
                    timeout=refresh_timeout,
                )
            except requests.Timeout:
                # A timeout never rejects the grant; only invalid_grant does.
                if call_deadline_remaining() <= 0 or timeout_was_budget_bound(
                    refresh_timeout, remaining
                ):
                    raise ClioCallBudgetExceededError() from None
                raise TokenRefreshError(
                    "Clio did not answer the token refresh in time"
                ) from None
            except requests.RequestException:
                raise TokenRefreshError(
                    "Clio token refresh failed; check connectivity and retry"
                ) from None
            if resp.status_code == 200:
                try:
                    new_tokens = stamp_token_payload(
                        resp.json(), previous_refresh_token=self.refresh_token
                    )
                except (requests.RequestException, ValueError, TypeError):
                    raise TokenRefreshError(
                        "Clio token refresh failed; check connectivity and retry"
                    ) from None
                replaced, _backend, generation = (
                    credentials.replace_token_cache_if_generation(
                        self.user_sub, new_tokens, self.generation
                    )
                )
                if not replaced:
                    self.tokens, self.generation = credentials.credential_snapshot(
                        self.user_sub
                    )
                    self.last_refresh_adopted_external_generation = True
                    return self.tokens
                self.tokens = new_tokens
                self.generation = generation
                return new_tokens

            try:
                error_code = resp.json().get("error")
            except (requests.RequestException, ValueError, AttributeError):
                error_code = None
            if error_code == "invalid_grant":
                marked = credentials.mark_credential_dead(
                    self.user_sub,
                    "refresh token rejected by Clio (invalid_grant)",
                    expected_generation=self.generation,
                )
                if not marked:
                    self.tokens, self.generation = credentials.credential_snapshot(
                        self.user_sub
                    )
                    return self.tokens
                raise CredentialDeadError(credentials.CREDENTIAL_DEAD_MESSAGE)
            raise TokenRefreshError(f"Token refresh failed ({resp.status_code})")


def _document_status(response):
    try:
        response.raise_for_status()
    except requests.RequestException:
        raise ClioDegradedError(
            "Clio document request failed; check the document and retry"
        ) from None


def _document_json(response):
    try:
        return response.json()
    except requests.RequestException:
        raise ClioDegradedError(
            "Clio document request failed; check the document and retry"
        ) from None


class ClioClient:
    def __init__(self, user_sub: str | None = None):
        self.user_sub = user_sub or _acting_user_sub()
        self.tm = TokenManager(self.user_sub)
        if not self.tm.access_token:
            raise ClioConnectionMissingError(
                "No Clio credentials are configured. Run clio-mcp-setup."
            )
        if credentials.credential_is_dead(self.user_sub):
            raise CredentialDeadError(credentials.CREDENTIAL_DEAD_MESSAGE)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.tm.access_token}",
                "Content-Type": "application/json",
            }
        )

    @staticmethod
    def _deadline_error() -> ClioDegradedError:
        """Name the deadline that actually expired so the caller can act on it."""
        if call_deadline_remaining() <= MIN_HTTP_BUDGET_SECONDS:
            return ClioCallBudgetExceededError()
        return ClioDegradedError(
            "Clio request deadline exceeded; retry the operation later."
        )

    def _request(self, method, path, params=None, json_body=None, retry=True):
        if credentials.credential_is_dead(self.user_sub):
            raise CredentialDeadError(credentials.CREDENTIAL_DEAD_MESSAGE)
        admitted_generation = self.tm.generation
        url = f"{BASE_URL}/{path.lstrip('/')}"
        # The whole call may have an optional deadline covering every paged
        # request, so the effective deadline is whichever expires first.
        deadline = min(
            time.monotonic() + API_REQUEST_DEADLINE_SECONDS,
            time.monotonic() + call_deadline_remaining(),
        )
        may_refresh = bool(retry)
        rate_limit_retries = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= MIN_HTTP_BUDGET_SECONDS:
                raise self._deadline_error()
            outbound_limiter = _rate_limiter_for(self.user_sub)
            if not outbound_limiter.acquire(max_wait_seconds=remaining):
                raise ClioDegradedError(
                    "Clio request rate limit wait exceeded the request deadline; "
                    "retry the operation later."
                )
            remaining = deadline - time.monotonic()
            if remaining <= MIN_HTTP_BUDGET_SECONDS:
                raise self._deadline_error()
            timeout = phase_timeouts(remaining)
            _ensure_current_authorization(self.user_sub, admitted_generation)
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                )
            except requests.Timeout:
                if call_deadline_remaining() <= 0 or timeout_was_budget_bound(
                    timeout, remaining
                ):
                    raise ClioCallBudgetExceededError() from None
                raise ClioDegradedError(
                    "Clio did not respond in time; retry the operation later."
                ) from None

            except requests.RequestException:
                raise ClioDegradedError(
                    "Clio request failed; check connectivity and retry"
                ) from None
            _ensure_current_authorization(self.user_sub, admitted_generation)
            if resp.status_code == 401:
                if may_refresh:
                    _ensure_current_authorization(self.user_sub, admitted_generation)
                    self.tm.refresh()
                    if self.tm.last_refresh_adopted_external_generation:
                        raise CredentialChangedError(CREDENTIAL_CHANGED_MESSAGE)
                    admitted_generation = self.tm.generation
                    self.session.headers["Authorization"] = (
                        f"Bearer {self.tm.access_token}"
                    )
                    may_refresh = False
                    continue
                logger.warning(
                    "Clio returned 401 after a successful token refresh; grant retained",
                    extra={"event": "clio_api_401_after_refresh_degraded"},
                )
                raise ClioDegradedError(
                    "Clio still rejected this API request after token refresh; "
                    "the connection was retained and the operation can be retried."
                )

            if resp.status_code != 429:
                return resp

            retry_after = resp.headers.get("Retry-After")
            try:
                requested_wait = float(retry_after) if retry_after is not None else 10.0
            except (TypeError, ValueError):
                requested_wait = 10.0
            if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                raise ClioDegradedError(
                    "Clio rate limiting persisted after bounded retries; "
                    "retry the operation later."
                )
            wait_seconds = max(0.0, min(requested_wait, MAX_RETRY_AFTER_SECONDS))
            remaining = deadline - time.monotonic()
            if wait_seconds >= remaining:
                raise ClioDegradedError(
                    "Clio rate limiting exceeded the request deadline; "
                    "retry the operation later."
                )
            rate_limit_retries += 1
            logger.warning(
                "Clio rate limited an API request; retrying with bounded backoff",
                extra={
                    "event": "clio_api_rate_limited",
                    "retry": rate_limit_retries,
                    "wait_seconds": wait_seconds,
                },
            )
            time.sleep(wait_seconds)

    def get(self, path, fields=None, **params):
        if fields:
            params["fields"] = fields
        return self._request("GET", path, params=params)

    def post(self, path, data, fields=None):
        params = {"fields": fields} if fields else None
        return self._request("POST", path, params=params, json_body={"data": data})

    def patch(self, path, data, fields=None):
        params = {"fields": fields} if fields else None
        return self._request("PATCH", path, params=params, json_body={"data": data})

    def delete(self, path):
        return self._request("DELETE", path)

    def _get_list(self, path, fields=None, *, max_records=None, **params):
        """Return up to five index pages, optionally capped at ``max_records``."""
        if fields:
            params["fields"] = fields
        record_limit = max_records
        request_params = dict(params)
        first_body: dict[str, Any] | None = None
        last_body: dict[str, Any] | None = None
        all_data: list[Any] = []
        next_url: str | None = None

        for _page_number in range(MAX_LIST_PAGES):
            resp = self.get(path, **request_params)
            body = resp.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                return body
            if first_body is None:
                first_body = dict(body)
            last_body = body
            page_data = body["data"]
            if isinstance(record_limit, int):
                remaining = max(0, record_limit - len(all_data))
                all_data.extend(page_data[:remaining])
            else:
                all_data.extend(page_data)
            raw_next = body.get("meta", {}).get("paging", {}).get("next")
            next_url = str(raw_next) if raw_next else None
            if not next_url or (
                isinstance(record_limit, int) and len(all_data) >= record_limit
            ):
                break
            parsed = urlparse(next_url)
            next_params = {
                key: values[-1]
                for key, values in parse_qs(
                    parsed.query, keep_blank_values=True
                ).items()
            }
            if next_params:
                request_params = {**request_params, **next_params}
            else:
                request_params = {**request_params, "page_token": next_url}

        result = first_body or {"data": []}
        result["data"] = all_data
        if last_body is not None and "meta" in last_body:
            result["meta"] = last_body["meta"]
        if next_url:
            result["truncated"] = True
            result["note"] = LIST_TRUNCATION_NOTE
        return result

    # ── Identity ──────────────────────────────────────────────────────────────

    def who_am_i(self):
        body = self.get("users/who_am_i", fields=_fields("who_am_i")).json()
        return body.get("data", body)

    # ── Matters ───────────────────────────────────────────────────────────────

    def list_matters(self, status="open", order="id(asc)", limit=25, **kwargs):
        return self._get_list(
            "matters",
            fields=_fields("list_matters"),
            max_records=limit,
            status=status,
            order=order,
            limit=limit,
            **kwargs,
        )

    def get_matter(self, matter_id):
        resp = self.get(f"matters/{matter_id}", fields=_fields("get_matter"))
        body = resp.json()
        return body.get("data", body)

    def create_matter(
        self,
        description,
        client_id,
        status="open",
        practice_area_id=None,
        responsible_attorney_id=None,
        billing_method=None,
        **kwargs,
    ):
        data = {
            "description": description,
            "client": {"id": client_id},
            "status": status,
        }
        if practice_area_id:
            data["practice_area"] = {"id": practice_area_id}
        if responsible_attorney_id:
            data["responsible_attorney"] = {"id": responsible_attorney_id}
        if billing_method:
            data["billing_method"] = billing_method
        data.update(kwargs)
        return self.post("matters", data, fields=_fields("create_matter")).json()

    def update_matter(
        self,
        matter_id,
        description=None,
        status=None,
        responsible_attorney_id=None,
        practice_area_id=None,
        close_date=None,
    ):
        data = {}
        if description:
            data["description"] = description
        if status:
            data["status"] = status
        if responsible_attorney_id:
            data["responsible_attorney"] = {"id": responsible_attorney_id}
        if practice_area_id:
            data["practice_area"] = {"id": practice_area_id}
        if close_date:
            data["close_date"] = close_date
        return self.patch(
            f"matters/{matter_id}", data, fields=_fields("update_matter")
        ).json()

    # ── Contacts ──────────────────────────────────────────────────────────────

    def list_contacts(self, query=None, contact_type=None, order="id(asc)", limit=25):
        params = {"limit": limit, "order": order}
        if query:
            params["query"] = query
        if contact_type:
            params["type"] = contact_type
        return self._get_list(
            "contacts",
            fields=_fields("list_contacts"),
            max_records=limit,
            **params,
        )

    def get_contact(self, contact_id):
        body = self.get(f"contacts/{contact_id}", fields=_fields("get_contact")).json()
        return body.get("data", body)

    def create_contact(
        self,
        first_name=None,
        last_name=None,
        name=None,
        contact_type="Person",
        email=None,
        phone=None,
        **kwargs,
    ):
        data: dict[str, Any] = {"type": contact_type}
        if contact_type == "Person":
            data["first_name"] = first_name
            data["last_name"] = last_name
        else:
            data["name"] = name
        if email:
            data["email_addresses"] = [{"address": email, "name": "Work"}]
        if phone:
            data["phone_numbers"] = [{"number": phone, "name": "Work"}]
        data.update(kwargs)
        return self.post("contacts", data, fields=_fields("create_contact")).json()

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def list_tasks(
        self,
        status="pending",
        matter_id=None,
        assignee_id=None,
        order="id(asc)",
        limit=25,
    ):
        params = {"limit": limit, "order": order, "status": status}
        if matter_id:
            params["matter_id"] = matter_id
        if assignee_id:
            params["assignee_id"] = assignee_id
        return self._get_list(
            "tasks", fields=_fields("list_tasks"), max_records=limit, **params
        )

    def create_task(
        self,
        name,
        description=None,
        due_at=None,
        priority="Normal",
        matter_id=None,
        assignee_id=None,
    ):
        if assignee_id is None:
            user = self.who_am_i()
            if isinstance(user, dict):
                assignee_id = user.get("id")
        # description is required by Clio API; fall back to name if caller omits it
        if not description:
            description = name
        data = {"name": name, "description": description, "priority": priority}
        if due_at:
            data["due_at"] = due_at
        if matter_id:
            data["matter"] = {"id": matter_id}
        if assignee_id:
            data["assignee"] = {"id": assignee_id, "type": "User"}
        return self.post("tasks", data, fields=_fields("create_task")).json()

    def update_task(
        self,
        task_id,
        name=None,
        due_at=None,
        priority=None,
        status=None,
        assignee_id=None,
    ):
        data = {}
        if name:
            data["name"] = name
        if due_at:
            data["due_at"] = due_at
        if priority:
            data["priority"] = priority
        if status:
            data["status"] = status
        if assignee_id:
            data["assignee"] = {"id": assignee_id, "type": "User"}
        return self.patch(
            f"tasks/{task_id}", data, fields=_fields("update_task")
        ).json()

    def complete_task(self, task_id):
        return self.update_task(task_id, status="complete")

    # ── Calendar ──────────────────────────────────────────────────────────────

    @staticmethod
    def _to_datetime(s: str, *, end_of_day: bool = False) -> str:
        """Clio calendar API requires full ISO 8601 datetime; bare YYYY-MM-DD is rejected."""
        if s and len(s) == 10:
            return s + ("T23:59:59Z" if end_of_day else "T00:00:00Z")
        return s

    def list_calendar_entries(self, start_date=None, end_date=None, limit=25):
        params: dict[str, Any] = {"limit": limit}
        if start_date:
            params["from"] = self._to_datetime(start_date)
        if end_date:
            params["to"] = self._to_datetime(end_date, end_of_day=True)
        return self._get_list(
            "calendar_entries",
            fields=_fields("list_calendar_entries"),
            max_records=limit,
            **params,
        )

    def _get_default_calendar_id(self):
        resp = self.get("calendars", fields="id,name", limit=1)
        body = resp.json()
        calendars = body.get("data", [])
        if calendars:
            return calendars[0]["id"]
        raise ValueError("No calendars found for this Clio account")

    def create_calendar_entry(
        self,
        summary,
        start_at,
        end_at,
        matter_id=None,
        description=None,
        location=None,
        attendee_ids=None,
        calendar_owner_id=None,
    ):
        start_at = self._to_datetime(start_at)
        end_at = self._to_datetime(end_at)
        if calendar_owner_id is None:
            calendar_owner_id = self._get_default_calendar_id()
        data = {
            "summary": summary,
            "start_at": start_at,
            "end_at": end_at,
            "calendar_owner": {"id": calendar_owner_id},
        }
        if matter_id:
            data["matter"] = {"id": matter_id}
        if description:
            data["description"] = description
        if location:
            data["location"] = location
        if attendee_ids:
            data["attendees"] = [{"id": aid, "type": "User"} for aid in attendee_ids]
        return self.post(
            "calendar_entries", data, fields=_fields("create_calendar_entry")
        ).json()

    # ── Activities (Time + Expenses) ──────────────────────────────────────────

    def list_activities(
        self,
        matter_id=None,
        user_id=None,
        activity_type=None,
        start_date=None,
        end_date=None,
        created_since=None,
        updated_since=None,
        order="id(asc)",
        limit=25,
    ):
        params: dict[str, Any] = {"limit": limit}
        if matter_id:
            params["matter_id"] = matter_id
        if user_id:
            params["user_id"] = user_id
        if activity_type:
            params["type"] = activity_type
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if created_since:
            params["created_since"] = created_since
        if updated_since:
            params["updated_since"] = updated_since
        params["order"] = order
        return self._get_list(
            "activities",
            fields=_fields("list_activities"),
            max_records=limit,
            **params,
        )

    def create_time_entry(
        self, date, quantity_seconds, matter_id, note=None, user_id=None, price=None
    ):
        data = {
            "type": "TimeEntry",
            "date": date,
            "quantity": quantity_seconds,
            "matter": {"id": matter_id},
        }
        if note:
            data["note"] = note
        if user_id:
            data["user"] = {"id": user_id}
        if price is not None:
            data["price"] = float(price)
        return self.post("activities", data, fields=_fields("create_time_entry")).json()

    def create_expense_entry(
        self,
        matter_id,
        amount_dollars,
        description,
        date=None,
        quantity=1,
        non_billable=False,
    ):
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = {
            "matter": {"id": matter_id},
            "quantity": quantity,
            "price": float(amount_dollars),
            "note": description,
            "date": date,
            "non_billable": non_billable,
            "type": "ExpenseEntry",
        }
        return self.post(
            "activities", data, fields=_fields("create_expense_entry")
        ).json()

    # ── Billing ───────────────────────────────────────────────────────────────

    def list_bills(
        self,
        status=None,
        matter_id=None,
        client_id=None,
        start_date=None,
        end_date=None,
        created_since=None,
        updated_since=None,
        overdue=False,
        limit=50,
    ):
        params: dict[str, Any] = {"limit": limit, "order": "issued_at(desc)"}
        if status == "overdue":
            overdue = True
        elif status:
            params["state"] = status
        if overdue:
            params["overdue_only"] = True
        if matter_id:
            params["matter_id"] = matter_id
        if client_id:
            params["client_id"] = client_id
        if start_date:
            params["issued_after"] = start_date
        if end_date:
            params["issued_before"] = end_date
        if created_since:
            params["created_since"] = created_since
        if updated_since:
            params["updated_since"] = updated_since
        return self._get_list("bills", fields=_fields("list_bills"), **params)

    # ── Documents ─────────────────────────────────────────────────────────────

    def list_documents(
        self,
        matter_id=None,
        query=None,
        created_since=None,
        updated_since=None,
        order=None,
        limit=50,
    ):
        params: dict[str, Any] = {"limit": limit}
        if matter_id:
            params["matter_id"] = matter_id
        if query:
            params["query"] = query
        if created_since:
            params["created_since"] = created_since
        if updated_since:
            params["updated_since"] = updated_since
        if order:
            params["order"] = order
        return self._get_list("documents", fields=_fields("list_documents"), **params)

    def upload_document(self, file_path, filename=None, matter_id=None, folder_id=None):
        import mimetypes

        with files.open_upload(file_path) as (source, size, local_name):
            if filename is None:
                filename = local_name
            doc_data = {"name": filename}
            if matter_id:
                doc_data["parent"] = {"id": matter_id, "type": "Matter"}
            elif folder_id:
                doc_data["parent"] = {"id": folder_id, "type": "Folder"}
            resp = self.post(
                "documents", doc_data, fields=_fields("upload_document_create")
            )
            _document_status(resp)
            body = _document_json(resp)
            doc_id = body["data"]["id"]
            put_url = body["data"]["latest_document_version"]["put_url"]
            if not put_url:
                raise TransferError(
                    f"No document upload URL returned. Created Clio document id: {doc_id}; delete the empty document if needed"
                )
            content_type = (
                mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
            try:
                transfer(
                    "PUT",
                    put_url,
                    source=source,
                    source_size=size,
                    content_type=content_type,
                )
            except TransferError as exc:
                raise TransferError(
                    f"{exc}. Created Clio document id: {doc_id}; delete the empty document if needed"
                ) from None

        return _document_json(
            self.patch(
                f"documents/{doc_id}",
                {"fully_uploaded": True},
                fields=_fields("upload_document_finalize"),
            )
        )

    def download_document(self, document_id, output_path=None):
        if output_path:
            files.validate_output(output_path)
        resp = self.get(f"documents/{document_id}", fields=_fields("download_document"))
        _document_status(resp)
        doc_data = _document_json(resp)["data"]
        version = doc_data.get("latest_document_version", {})
        download_url = version.get("download_url")
        filename = version.get("filename") or doc_data.get("name", "document")
        if not download_url:
            raise TransferError("No document download URL returned")
        if output_path:
            with files.download_target(output_path) as sink:
                transfer("GET", download_url, sink=sink)
            return output_path
        return filename, transfer("GET", download_url)

    # ── Notes ─────────────────────────────────────────────────────────────────

    def list_notes(
        self,
        matter_id=None,
        contact_id=None,
        created_since=None,
        updated_since=None,
        order=None,
        limit=50,
    ):
        params: dict[str, Any] = {"limit": limit}
        if contact_id:
            params["type"] = "contact"
            params["contact_id"] = contact_id
        else:
            params["type"] = "matter"
            if matter_id:
                params["matter_id"] = matter_id
        if created_since:
            params["created_since"] = created_since
        if updated_since:
            params["updated_since"] = updated_since
        if order:
            params["order"] = order
        return self._get_list("notes", fields=_fields("list_notes"), **params)

    def create_note(
        self, subject, detail, note_type=None, matter_id=None, contact_id=None
    ):
        if (matter_id is None) == (contact_id is None):
            raise ValueError(
                "Exactly one of matter_id or contact_id must be provided for a note."
            )
        association_type = "matter" if matter_id is not None else "contact"
        if note_type is not None and note_type.lower() != association_type:
            raise ValueError(
                f"note_type {note_type!r} conflicts with the {association_type} "
                "association."
            )

        data = {"subject": subject, "detail": detail, "type": association_type}
        if matter_id is not None:
            data["matter"] = {"id": matter_id}
        else:
            data["contact"] = {"id": contact_id}
        return self.post("notes", data, fields=_fields("create_note")).json()

    # ── Utility ───────────────────────────────────────────────────────────────

    def list_practice_areas(self, limit=200):
        return self._get_list(
            "practice_areas", fields=_fields("list_practice_areas"), limit=limit
        )

    def list_users(self, limit=50):
        return self._get_list("users", fields=_fields("list_users"), limit=limit)

    def list_communications(
        self,
        matter_id=None,
        received_since=None,
        received_before=None,
        created_since=None,
        updated_since=None,
        order=None,
        limit=50,
    ):
        params: dict[str, Any] = {"limit": limit}
        if matter_id:
            params["matter_id"] = matter_id
        if received_since:
            params["received_since"] = received_since
        if received_before:
            params["received_before"] = received_before
        if created_since:
            params["created_since"] = created_since
        if updated_since:
            params["updated_since"] = updated_since
        if order:
            params["order"] = order
        return self._get_list(
            "communications", fields=_fields("list_communications"), **params
        )

    def add_relationship(self, matter_id, contact_id, description):
        data = {
            "matter": {"id": matter_id},
            "contact": {"id": contact_id},
            "description": description,
        }
        return self.post(
            "relationships", data, fields=_fields("add_relationship")
        ).json()

    # ── Timers ────────────────────────────────────────────────────────────────

    def list_timers(self):
        # Clio Timer resource does not expose matter or user as expandable fields.
        # Returns 404 when no timer is running — that is a normal state, not an error.
        resp = self.get("timer", fields=_fields("list_timers"))
        if resp.status_code == 404:
            return {"data": None}
        return resp.json()

    def start_timer(self, matter_id):
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        activity_resp = self.post(
            "activities",
            {
                "type": "TimeEntry",
                "date": today,
                "matter": {"id": matter_id},
            },
        )
        try:
            activity = activity_resp.json()
        except ValueError:
            return {"error": "Failed to create time entry for timer"}
        activity_id = (activity.get("data") or activity).get("id")
        if not activity_id:
            return {"error": f"Could not get activity id: {activity}"}
        return self.post(
            "timer", {"activity": {"id": activity_id}}, fields=_fields("start_timer")
        ).json()

    def stop_timer(self):
        # Clio has one timer per user — DELETE /timer.json, no path param
        resp = self.delete("timer")
        try:
            return resp.json()
        except ValueError:
            return {"stopped": True}

    # ── Bills (create) ────────────────────────────────────────────────────────

    def create_bill(self, matter_id, subject=None, issued_at=None, due_at=None):
        return {
            "status": "not_supported",
            "message": "Clio API v4 does not support bill creation. Generate bills through the Clio web UI: Billing > Generate Bills for Matter.",
        }
