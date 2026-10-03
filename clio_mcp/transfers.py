"""Bounded HTTPS storage transfers using Requests trust stores and proxies."""

import ipaddress
import math
import os
import queue
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

from clio_mcp.files import max_bytes

TRANSFER_DEADLINE_SECONDS = 120
MIN_BYTES_PER_SECOND = 128 * 1024
READ_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 3
CHUNK_BYTES = 64 * 1024


class TransferError(RuntimeError):
    """Safe transfer error; never includes a URL, query, body, or chained error."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TransferError("Document transfer exceeded its total deadline")
    return remaining


def _deadline_seconds(size):
    raw = os.environ.get("CLIO_MCP_TRANSFER_DEADLINE_SECONDS")
    try:
        value = (
            float(raw)
            if raw is not None
            else TRANSFER_DEADLINE_SECONDS + size / MIN_BYTES_PER_SECOND
        )
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise TransferError(
            "CLIO_MCP_TRANSFER_DEADLINE_SECONDS must be a positive number"
        ) from None


def _public(address):
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.version == 4 and (
        ip in ipaddress.ip_network("100.64.0.0/10")
        or ip in ipaddress.ip_network("198.18.0.0/15")
    ):
        return True
    return ip.is_global and not ip.is_multicast


def _destination(url, deadline, *, proxied=False):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        if any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or 443
        if not proxied and (
            host.lower() == "localhost" or host.lower().endswith(".localhost")
        ):
            raise ValueError
    except (ValueError, UnicodeError, AttributeError, TypeError):
        raise TransferError(
            "Document transfer requires a public HTTPS destination"
        ) from None
    if proxied:
        return parsed, host, port
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TransferError("Document transfer exceeded its total deadline") from None
    if not addresses or any(not _public(item[4][0]) for item in addresses):
        raise TransferError(
            "Document transfer destination must resolve only to public addresses"
        )
    return parsed, host, port


class _NoStorageAuth(requests.auth.AuthBase):
    def __call__(self, request):
        # Keep environment CA/proxy support while suppressing implicit netrc auth.
        request.headers.pop("Authorization", None)
        request.headers.pop("Cookie", None)
        return request


class _UploadBody:
    def __init__(self, source, size, deadline, cancelled):
        self.source, self.size = source, size
        self.deadline, self.cancelled = deadline, cancelled
        self.sent = 0
        source.seek(0)

    def __len__(self):
        return self.size

    def read(self, size=-1):
        _remaining(self.deadline)
        if self.cancelled.is_set():
            raise TransferError("Document transfer exceeded its total deadline")
        if self.sent == self.size:
            if self.source.read(1):
                raise TransferError("Document changed during upload")
            return b""
        chunk = self.source.read(
            min(CHUNK_BYTES, self.size - self.sent, size if size > 0 else CHUNK_BYTES)
        )
        if not chunk:
            raise TransferError("Document changed during upload")
        self.sent += len(chunk)
        if self.sent == self.size and self.source.read(1):
            raise TransferError("Document changed during upload")
        return chunk


def transfer(method, url, *, source=None, source_size=0, content_type=None, sink=None):
    """Stream bounded chunks; the caller's deadline also covers DNS and headers."""
    limit = max_bytes()
    if source_size > limit or source_size < 0:
        raise TransferError("Document exceeds CLIO_MCP_MAX_FILE_BYTES")
    # Downloads with unknown length budget for the maximum permitted size.
    started = time.monotonic()
    deadline = started + _deadline_seconds(source_size if source is not None else limit)
    cancelled = threading.Event()
    results = queue.Queue(maxsize=2)

    def deliver(kind, value):
        while not cancelled.is_set() and time.monotonic() < deadline:
            try:
                results.put((kind, value), timeout=min(0.1, _remaining(deadline)))
                return
            except queue.Full:
                pass

    def run():
        nonlocal url, deadline
        try:
            with requests.Session() as session:
                session.auth = _NoStorageAuth()
                session.headers.clear()
                for redirect in range(MAX_REDIRECTS + 1):
                    settings = session.merge_environment_settings(
                        url, {}, True, None, None
                    )
                    proxied = bool(
                        requests.utils.select_proxy(url, settings["proxies"])
                    )
                    _destination(url, deadline, proxied=proxied)
                    remaining = _remaining(deadline)
                    headers = {"Accept-Encoding": "identity"}
                    body = None
                    if source is not None:
                        headers.update(
                            {
                                "Content-Length": str(source_size),
                                "Content-Type": content_type
                                or "application/octet-stream",
                            }
                        )
                        body = _UploadBody(source, source_size, deadline, cancelled)
                    session.cookies.clear()
                    with session.request(
                        method,
                        url,
                        data=body,
                        headers=headers,
                        allow_redirects=False,
                        timeout=(
                            min(10, remaining),
                            min(READ_TIMEOUT_SECONDS, remaining),
                        ),
                        **settings,
                    ) as response:
                        _remaining(deadline)
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if redirect == MAX_REDIRECTS or not location:
                                raise TransferError(
                                    "Document transfer redirect limit exceeded"
                                )
                            if method != "GET" and response.status_code not in (
                                307,
                                308,
                            ):
                                raise TransferError("Unsafe document upload redirect")
                            url_next = urljoin(url, location)
                            url = url_next
                            continue
                        if not 200 <= response.status_code < 300:
                            raise TransferError(
                                f"Document transfer failed (HTTP {response.status_code})"
                            )
                        length = response.headers.get("Content-Length")
                        length = int(length) if length is not None else None
                        if length is not None and (length < 0 or length > limit):
                            raise TransferError(
                                "Document exceeds CLIO_MCP_MAX_FILE_BYTES"
                            )
                        if source is None and length is not None:
                            deadline = started + _deadline_seconds(length)
                            _remaining(deadline)
                        count = 0
                        for chunk in response.iter_content(CHUNK_BYTES):
                            _remaining(deadline)
                            if cancelled.is_set():
                                return
                            count += len(chunk)
                            if count > limit:
                                raise TransferError(
                                    "Document exceeds CLIO_MCP_MAX_FILE_BYTES"
                                )
                            deliver("chunk", chunk)
                        if length is not None and count != length:
                            raise TransferError("Document transfer was incomplete")
                        deliver("done", count)
                        return
        except TransferError as exc:
            # Send only the safe message across the thread boundary, no traceback.
            deliver("error", str(exc))
        except Exception:
            message = (
                "Document transfer exceeded its total deadline"
                if time.monotonic() >= deadline
                else "Document transfer failed; check connectivity and retry"
            )
            deliver("error", message)

    # Requests owns TLS/proxy/connect behavior. A bounded queue keeps the body
    # streamed and the caller deadline strict even during trickling headers.
    # Only the caller writes the local destination; cancelled workers cannot.
    threading.Thread(target=run, daemon=True).start()
    try:
        while True:
            try:
                kind, value = results.get(timeout=min(0.1, _remaining(deadline)))
            except queue.Empty:
                # A download may acquire a shorter deadline after its headers.
                continue
            _remaining(deadline)
            if kind == "error":
                raise TransferError(value) from None
            if kind == "done":
                return value
            if sink is not None:
                try:
                    sink.write(value)
                except OSError:
                    raise TransferError(
                        "Cannot save downloaded document; check the destination filesystem"
                    ) from None
    finally:
        cancelled.set()
