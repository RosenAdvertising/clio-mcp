#!/usr/bin/env python3
"""Local browser authorization for the user's own Clio application."""

import argparse
import base64
import getpass
import hashlib
import hmac
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from clio_mcp import credentials

REDIRECT_URI = "http://127.0.0.1:8765/callback"
AUTH_URL = "https://app.clio.com/oauth/authorize"
TOKEN_URL = "https://app.clio.com/oauth/token"
CALLBACK_WAIT_SECONDS = 600
FULL_SCOPE = (
    "contacts.read contacts.write "
    "matters.read matters.write "
    "activities.read activities.write "
    "bills.read "
    "documents.read documents.write "
    "notes.read notes.write "
    "calendar_entries.read calendar_entries.write "
    "tasks.read tasks.write "
    "communications.read "
    "timer.read timer.write "
    "users.read "
    "practice_areas.read"
)


class OAuthSetupError(RuntimeError):
    """Safe setup error with no provider body or callback query."""


class _CallbackHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        remaining = max(0.001, self.server.deadline - time.monotonic())
        self.connection.settimeout(min(2, remaining))
        self._timer = threading.Timer(min(2, remaining), self._abort)
        self._timer.daemon = True
        self._timer.start()

    def _abort(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self._timer.cancel()
        super().finish()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        status = 400
        message = b"Invalid authorization callback."
        if parsed.path != "/callback":
            status = 404
        elif self.server.completed:
            status = 409
        elif len(params.get("state", [])) != 1 or not hmac.compare_digest(
            params["state"][0].encode(), self.server.state.encode()
        ):
            message = b"Authorization state did not match."
        elif "error" in params:
            self.server.completed = True
            self.server.oauth_error = True
            message = b"Clio authorization was denied or failed. Run setup again."
        elif len(params.get("code", [])) == 1 and params["code"][0]:
            self.server.code = params["code"][0]
            self.server.completed = True
            status = 200
            message = b"Authorization complete. You can close this tab."
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(message)

    def log_message(self, *args):
        pass


class _CallbackServer(HTTPServer):
    def handle_error(self, request, client_address):
        # Callback queries may contain codes; never emit tracebacks/request data.
        pass


def authorize(client_id: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    # The existing Clio authorization/exchange implementation already sends S256.
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": FULL_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    try:
        server = _CallbackServer(("127.0.0.1", 8765), _CallbackHandler)
    except OSError:
        raise OAuthSetupError(
            "Cannot bind local callback port 8765; close the other listener and retry"
        ) from None
    try:
        server.state = state
        server.code = None
        server.completed = False
        server.oauth_error = False
        server.deadline = time.monotonic() + CALLBACK_WAIT_SECONDS
        url = f"{AUTH_URL}?{urlencode(params)}"
        print("\nOpening browser for Clio authorization...")
        print(f"Open this authorization URL in your browser:\n{url}")
        webbrowser.open(url)
        print("Waiting for Clio to redirect back (port 8765)...")
        while not server.completed and time.monotonic() < server.deadline:
            server.timeout = min(1, max(0.001, server.deadline - time.monotonic()))
            server.handle_request()
        if server.oauth_error:
            raise OAuthSetupError(
                "Clio authorization was denied or failed; run setup again"
            )
        if not server.code:
            raise OAuthSetupError(
                "Timed out waiting for Clio authorization; run setup again"
            )
        return server.code, verifier
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(
        description="Authorize your own Clio developer application locally."
    )
    parser.add_argument("--client-id", default=None)
    args = parser.parse_args()
    print("=== clio-mcp OAuth Setup ===\n")
    default_id = credentials.get_secret("CLIO_CLIENT_ID")
    prompt = f"Clio Client ID [{default_id}]: " if default_id else "Clio Client ID: "
    client_id = args.client_id or input(prompt).strip() or default_id
    # Re-setup always requests a secret; a new ID must never use the old one.
    client_secret = getpass.getpass("Clio Client Secret: ").strip()
    if not client_id or not client_secret:
        print("Error: Client ID and Secret are required.")
        sys.exit(1)
    from clio_mcp.client import stamp_token_payload

    try:
        code, verifier = authorize(client_id)
        print("Exchanging code for tokens...")
        try:
            with requests.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": REDIRECT_URI,
                },
                timeout=(10, 30),
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise OAuthSetupError(
                        f"Token exchange failed (HTTP {response.status_code}); check your application and retry"
                    )
                payload = response.json()
        except (requests.RequestException, ValueError):
            raise OAuthSetupError(
                "Token exchange failed; check your connection and retry"
            ) from None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise OAuthSetupError(
                "Clio did not return an access token; run setup again"
            )
        tokens = stamp_token_payload(payload)
        id_backend = credentials.set_secret("CLIO_CLIENT_ID", client_id)
        secret_backend = credentials.set_secret("CLIO_CLIENT_SECRET", client_secret)
        token_backend = credentials.commit_authorization_grant(
            credentials.LOCAL_SUBJECT, credentials.LOCAL_EMAIL, tokens
        )
        print(f"\nClient ID stored ({id_backend})")
        print(f"Client secret stored ({secret_backend})")
        print(f"Tokens stored ({token_backend})")
        print("\nRun 'clio-mcp-verify' to test the connection.")
    except OAuthSetupError as exc:
        print(f"Setup failed: {exc}")
        sys.exit(1)
    except (
        credentials.CredentialStoreConfigError,
        credentials.CredentialStateClearError,
        OSError,
    ):
        # Avoid exception context/provider data in stderr, while giving safe guidance.
        print(
            "Setup failed: authorization or private credential storage could not complete. Check the callback port, keyring and file permissions, then retry."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
