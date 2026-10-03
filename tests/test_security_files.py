"""Confined paths, atomic no-clobber publication and public HTTPS transfers."""

import io
import socket
import stat
import time
from pathlib import Path

import pytest

from clio_mcp import credentials, files, transfers


@pytest.mark.parametrize(
    "kind", ["sibling", "traversal", "outside", "credentials", "ssh", "aws"]
)
def test_upload_rejects_paths_outside_dedicated_directory(tmp_path, monkeypatch, kind):
    root = files.files_directory()
    paths = {
        "sibling": root.with_name(root.name + "-evil") / "data",
        "traversal": root / ".." / "data",
        "outside": tmp_path / "private-data",
        "credentials": credentials.CONFIG_DIR / ".env",
        "ssh": Path.home() / ".ssh" / "id_key",
        "aws": Path.home() / ".aws" / "credentials",
    }
    with pytest.raises(files.DocumentFileError):
        with files.open_upload(str(paths[kind])):
            pytest.fail("Unsafe upload was opened")


def test_directory_is_private_and_upload_caps_bytes_and_rejects_symlinks(
    tmp_path, monkeypatch
):
    root = files.files_directory()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    path = root / "upload"
    path.write_bytes(b"fixture")
    with files.open_upload("upload") as (stream, size, name):
        assert stream.read() == b"fixture" and size == 7 and name == "upload"
    (root / "link").symlink_to(path)
    (root / "dir-link").symlink_to(root, target_is_directory=True)
    for value in ["link", "dir-link/upload"]:
        with pytest.raises(files.DocumentFileError):
            with files.open_upload(value):
                pass
    monkeypatch.setenv("CLIO_MCP_MAX_FILE_BYTES", "6")
    with pytest.raises(files.DocumentFileError, match="exceeds"):
        with files.open_upload("upload"):
            pass
    with pytest.raises(files.DocumentFileError):
        with files.open_upload(str(root)):
            pass


def test_upload_rejects_fifo_and_hardlinks_without_blocking():
    import os

    root = files.files_directory()
    (root / "source").write_bytes(b"data")
    os.link(root / "source", root / "hardlink")
    os.mkfifo(root / "fifo")
    for name in ("hardlink", "fifo"):
        with pytest.raises(files.DocumentFileError):
            with files.open_upload(name):
                pass


def test_no_follow_open_rejects_replacement_after_validation(monkeypatch):
    root = files.files_directory()
    path = root / "source"
    path.write_bytes(b"data")
    outside = root.parent / "outside"
    outside.write_bytes(b"private")
    original = files.os.open

    def racing_open(name, flags, *args, **kwargs):
        if name == "source":
            path.unlink()
            path.symlink_to(outside)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", racing_open)
    with pytest.raises(files.DocumentFileError):
        with files.open_upload("source"):
            pass


def test_download_is_atomic_private_never_overwrites_and_cleans_up():
    root = files.files_directory()
    destination = root / "document"
    with files.download_target("document") as stream:
        stream.write(b"data")
        assert not destination.exists()
    assert destination.read_bytes() == b"data"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(files.DocumentFileError, match="overwriting"):
        with files.download_target("document"):
            pass
    with pytest.raises(RuntimeError):
        with files.download_target("interrupted") as stream:
            stream.write(b"partial")
            raise RuntimeError("fixture")
    assert not (root / "interrupted").exists()
    assert not list(root.glob(".download-*"))
    with pytest.raises(files.DocumentFileError):
        with files.download_target("race") as stream:
            stream.write(b"download")
            (root / "race").write_bytes(b"existing")
    assert (root / "race").read_bytes() == b"existing"
    assert not list(root.glob(".download-*"))


@pytest.fixture
def inode_changing_fchmod(monkeypatch):
    """Emulate exFAT/FAT inode changes and lack of hard-link support."""
    import errno

    original_fchmod = files.os.fchmod
    original_fstat = files.os.fstat
    original_stat = files.os.stat
    changed = set()

    def changed_identity(info):
        if (info.st_dev, info.st_ino) in changed:
            values = list(info)
            values[1] = info.st_ino ^ (1 << 60)
            return files.os.stat_result(values)
        return info

    def fchmod(fd, mode):
        original_fchmod(fd, mode)
        info = original_fstat(fd)
        changed.add((info.st_dev, info.st_ino))

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.ENOTSUP, "Fixture filesystem has no hard links")

    monkeypatch.setattr(files.os, "fchmod", fchmod)
    monkeypatch.setattr(
        files.os, "fstat", lambda fd: changed_identity(original_fstat(fd))
    )
    monkeypatch.setattr(
        files.os, "stat", lambda *a, **kw: changed_identity(original_stat(*a, **kw))
    )
    monkeypatch.setattr(files.os, "link", unsupported_link)
    return changed


def test_download_succeeds_when_fchmod_changes_reservation_inode(inode_changing_fchmod):
    root = files.files_directory()
    with files.download_target("document") as stream:
        stream.write(b"downloaded data")
    assert len(inode_changing_fchmod) == 2  # Temporary file and reservation.
    assert (root / "document").read_bytes() == b"downloaded data"
    assert stat.S_IMODE((root / "document").stat().st_mode) == 0o600
    assert list(root.iterdir()) == [root / "document"]


def test_failed_download_removes_inode_changed_reservation(
    monkeypatch, inode_changing_fchmod
):
    import errno

    root = files.files_directory()
    original_replace = files.os.replace

    def failed_replace(*args, **kwargs):
        raise OSError(errno.EIO, "Fixture publication failure")

    monkeypatch.setattr(files.os, "replace", failed_replace)
    with pytest.raises(files.DocumentFileError) as error:
        with files.download_target("document") as stream:
            stream.write(b"downloaded data")
    assert len(inode_changing_fchmod) == 2
    assert list(root.iterdir()) == []
    assert "Cannot save document safely" in str(error.value)
    monkeypatch.setattr(files.os, "replace", original_replace)
    with files.download_target("document") as stream:
        stream.write(b"retry data")
    assert (root / "document").read_bytes() == b"retry data"
    assert list(root.iterdir()) == [root / "document"]


@pytest.mark.parametrize(
    "root_kind", ["home", "config", "inside-config", "ssh", "aws", "link"]
)
def test_files_directory_cannot_overlap_sensitive_directories(
    monkeypatch, tmp_path, root_kind
):
    link = tmp_path / "link-root"
    (tmp_path / "actual").mkdir()
    link.symlink_to(tmp_path / "actual")
    choices = {
        "home": Path.home(),
        "config": credentials.CONFIG_DIR,
        "inside-config": credentials.CONFIG_DIR / "files",
        "ssh": Path.home() / ".ssh",
        "aws": Path.home() / ".aws",
        "link": link,
    }
    monkeypatch.setenv("CLIO_MCP_FILES_DIR", str(choices[root_kind]))
    with pytest.raises(files.DocumentFileError):
        files.files_directory()


def public_dns(monkeypatch, ip="93.184.216.34"):
    monkeypatch.setattr(
        transfers.socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/file",
        "file:///data",
        "https://localhost/file",
        "https://user:secret@example.com/file",
        "https://example.com:bad/file",
    ],
)
def test_transfer_rejects_non_public_https_urls(monkeypatch, url):
    public_dns(monkeypatch)
    with pytest.raises(transfers.TransferError):
        transfers._destination(url, time.monotonic() + 1)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ],
)
def test_transfer_rejects_private_and_special_dns_answers(monkeypatch, ip):
    public_dns(monkeypatch, ip)
    with pytest.raises(transfers.TransferError, match="public addresses"):
        transfers._destination(
            "https://example.com/signed?secret=fixture", time.monotonic() + 1
        )


class Response:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status = self.status_code = status
        self.headers = headers or {}
        self.chunks = iter(chunks or [])
        self.closed = False
        self.aborted = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read1(self, size):
        return next(self.chunks, b"")

    def iter_content(self, size):
        while True:
            chunk = self.read1(size)
            if not chunk:
                return
            yield chunk


def fake_connections(monkeypatch, responses):
    connections = []

    class Session:
        def __init__(self):
            self.headers = {}
            self.cookies = type("Cookies", (), {"clear": lambda self: None})()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def merge_environment_settings(self, *a):
            return {"proxies": {}, "stream": True, "verify": True, "cert": None}

        def request(self, method, url, **kwargs):
            response = responses.pop(0)
            response.host = transfers.urlsplit(url).hostname
            response.sent = []
            response.request_headers = kwargs["headers"]
            response.timeout = kwargs["timeout"]
            body = kwargs.get("data")
            connections.append(response)
            if body is not None:
                while chunk := body.read(65536):
                    response.sent.append(chunk)
            return response

    monkeypatch.setattr(transfers.requests, "Session", Session)
    return connections


def test_download_streams_counts_caps_and_revalidates_redirects(monkeypatch):
    public_dns(monkeypatch)
    sink = io.BytesIO()
    conns = fake_connections(
        monkeypatch,
        [
            Response(
                302, {"Location": "https://second.example/file?credential=fixture"}
            ),
            Response(chunks=[b"ab", b"cd"]),
        ],
    )
    assert transfers.transfer("GET", "https://first.example/file", sink=sink) == 4
    assert sink.getvalue() == b"abcd"
    assert [c.host for c in conns] == ["first.example", "second.example"]
    assert all(c.closed for c in conns)
    monkeypatch.setenv("CLIO_MCP_MAX_FILE_BYTES", "3")
    conns = fake_connections(monkeypatch, [Response(chunks=[b"ab", b"cd"])])
    with pytest.raises(transfers.TransferError, match="exceeds"):
        transfers.transfer("GET", "https://example.com/file")
    assert conns[0].closed


def test_redirect_to_private_host_and_redirect_loop_are_blocked(monkeypatch):
    public_dns(monkeypatch)
    conns = fake_connections(
        monkeypatch,
        [Response(302, {"Location": "https://localhost/admin?secret=fixture"})],
    )
    with pytest.raises(transfers.TransferError):
        transfers.transfer("GET", "https://example.com/file")
    assert len(conns) == 1 and conns[0].closed
    conns = fake_connections(
        monkeypatch, [Response(302, {"Location": "/again"}) for _ in range(4)]
    )
    with pytest.raises(transfers.TransferError, match="redirect limit"):
        transfers.transfer("GET", "https://example.com/file")
    assert len(conns) == 4 and all(c.closed for c in conns)


@pytest.mark.parametrize(
    "response",
    [
        Response(403, chunks=[b"private-body"]),
        Response(200, {"Content-Length": "1000"}),
        Response(200, {"Content-Length": "4"}, [b"ab"]),
    ],
)
def test_transfer_errors_do_not_expose_query_or_body(monkeypatch, caplog, response):
    public_dns(monkeypatch)
    monkeypatch.setenv("CLIO_MCP_MAX_FILE_BYTES", "100")
    fake_connections(monkeypatch, [response])
    with pytest.raises(transfers.TransferError) as error:
        transfers.transfer("GET", "https://example.com/file?credential=fixture-secret")
    assert "fixture-secret" not in str(error.value) + caplog.text
    assert "private-body" not in str(error.value) + caplog.text


def test_transfer_deadline_aborts_continuously_trickling_body(monkeypatch):
    public_dns(monkeypatch)

    class Trickle(Response):
        def read1(self, size):
            time.sleep(0.01)
            return b"x"

    conns = fake_connections(monkeypatch, [Trickle()])
    monkeypatch.setenv("CLIO_MCP_TRANSFER_DEADLINE_SECONDS", "0.025")
    start = time.monotonic()
    with pytest.raises(transfers.TransferError, match="deadline"):
        transfers.transfer("GET", "https://example.com/file")
    assert time.monotonic() - start < 0.2
    time.sleep(0.03)
    assert conns[0].closed


def test_dns_is_bounded_and_requests_connect_tries_multiple_addresses(monkeypatch):
    from urllib3.util import connection

    public_dns(monkeypatch)
    assert (
        transfers._destination("https://example.com/file", time.monotonic() + 1)[1]
        == "example.com"
    )
    attempts = []

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, dest):
            attempts.append(dest)
            if dest[0] == "93.184.216.33":
                raise ConnectionRefusedError

        def close(self):
            pass

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ["93.184.216.33", "93.184.216.34"]
        ],
    )
    monkeypatch.setattr(socket, "socket", lambda *a: Sock())
    connection.create_connection(("example.com", 443), timeout=1)
    assert attempts == [("93.184.216.33", 443), ("93.184.216.34", 443)]
    monkeypatch.setattr(
        transfers.socket, "getaddrinfo", lambda *a, **kw: time.sleep(0.1)
    )
    with pytest.raises(transfers.TransferError, match="deadline"):
        transfers._destination("https://example.com/file", time.monotonic() + 0.01)


def test_upload_streams_without_auth_headers_and_rejects_growth(monkeypatch):
    public_dns(monkeypatch)
    conns = fake_connections(monkeypatch, [Response()])
    assert (
        transfers.transfer(
            "PUT", "https://example.com/file", source=io.BytesIO(b"data"), source_size=4
        )
        == 0
    )
    assert conns[0].sent == [b"data"]
    fake_connections(monkeypatch, [Response()])
    with pytest.raises(transfers.TransferError, match="changed"):
        transfers.transfer(
            "PUT",
            "https://example.com/file",
            source=io.BytesIO(b"grown"),
            source_size=4,
        )


def test_real_tls_deadline_interrupts_slow_response_headers(tmp_path, monkeypatch):
    """The watchdog must interrupt a blocking header read, not just chunk loops."""
    import ssl
    import threading
    from datetime import datetime, timedelta, timezone
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "fixture-cert.pem"
    key_path = tmp_path / "fixture-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                # Every byte resets the socket's inactivity clock, but the total
                # deadline must still interrupt getresponse's header parsing.
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx":
                    if stopped.wait(0.02):
                        break
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
            except (OSError, ssl.SSLError):
                pass

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    port = server.server_address[1]
    # Only this fixture bypasses public destination validation for local TLS.
    monkeypatch.setattr(transfers, "_destination", lambda *a, **k: None)
    monkeypatch.setenv("CLIO_MCP_TRANSFER_DEADLINE_SECONDS", "0.15")
    start = time.monotonic()
    try:
        with pytest.raises(transfers.TransferError, match="deadline"):
            transfers.transfer("GET", f"https://localhost:{port}/file?fixture=signed")
        assert time.monotonic() - start < 0.5
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_download_known_length_reduces_total_deadline(monkeypatch):
    public_dns(monkeypatch)

    class SlowEmpty(Response):
        def read1(self, size):
            time.sleep(0.3)
            return b""

    fake_connections(monkeypatch, [SlowEmpty(headers={"Content-Length": "0"})])
    monkeypatch.setattr(transfers, "TRANSFER_DEADLINE_SECONDS", 0.025)
    started = time.monotonic()
    with pytest.raises(transfers.TransferError, match="deadline"):
        transfers.transfer("GET", "https://example.com/file")
    assert time.monotonic() - started < 0.2
