"""All automated tests use isolated local storage and reject external sockets."""

import importlib
import pytest
import ipaddress
import socket
import os
import tempfile
from pathlib import Path
import atexit

_import_home = tempfile.TemporaryDirectory(prefix="clio-test-home-")
atexit.register(_import_home.cleanup)
os.environ["HOME"] = _import_home.name
os.environ["CLIO_MCP_HOME"] = str(Path(_import_home.name) / ".clio-mcp")
os.environ["CLIO_MCP_USE_KEYRING"] = "0"
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
for _key in (
    "CLIO_CLIENT_ID",
    "CLIO_CLIENT_SECRET",
    "CLIO_MCP_TOKEN_KEY",
    "CLIO_MCP_TOKEN_KEY_RECOVERY",
    "CLIO_MCP_FILES_DIR",
):
    os.environ.pop(_key, None)

credentials = importlib.import_module("clio_mcp.credentials")
clio_client = importlib.import_module("clio_mcp.client")


@pytest.fixture(autouse=True)
def offline_local_store(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIO_MCP_TRANSFER_DEADLINE_SECONDS", raising=False)
    monkeypatch.setenv("CLIO_MCP_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("CLIO_MCP_USE_KEYRING", "0")
    monkeypatch.delenv("CLIO_MCP_TOKEN_KEY", raising=False)
    monkeypatch.delenv("CLIO_MCP_TOKEN_KEY_RECOVERY", raising=False)
    monkeypatch.delenv("CLIO_CLIENT_ID", raising=False)
    monkeypatch.delenv("CLIO_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_DIR", tmp_path / "config")
    for name, filename in (
        ("ENV_FILE", ".env"),
        ("TOKEN_FILE", "tokens.json"),
        ("CREDENTIAL_STATE_FILE", "credential_state.json"),
    ):
        monkeypatch.setattr(credentials, name, tmp_path / "config" / filename)
    credentials._refresh_locks.clear()
    clio_client._RATE_LIMITERS.clear()
    clio_client.set_call_deadline(None)
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guard(address):
        if isinstance(address, tuple):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed = address[0] == "localhost"
            if not allowed:
                raise AssertionError(
                    "External networking is forbidden in offline tests"
                )

    def checked_connect(sock, address):
        guard(address)
        return connect(sock, address)

    def checked_connect_ex(sock, address):
        guard(address)
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", checked_connect_ex)
    yield
    clio_client.set_call_deadline(None)
    clio_client._RATE_LIMITERS.clear()
