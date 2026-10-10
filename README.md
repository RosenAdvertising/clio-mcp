# clio-mcp

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

MCP server for [Clio Manage](https://www.clio.com) — matters, contacts, tasks,
calendar, time, expenses, billing, documents, and notes from an MCP-compatible
client such as Claude Desktop. Runs locally over stdio.

## Requirements

- Python 3.10+
- A Clio Manage account with API access
- Your own Clio developer application and its Client ID and Client Secret
- An MCP-compatible client

## Installation

```bash
pip install git+https://github.com/RosenAdvertising/clio-mcp
```

Run directly from source with uv:

```bash
uvx --from git+https://github.com/RosenAdvertising/clio-mcp clio-mcp
```

These commands require the source repository to be available. No PyPI release
is assumed. For a source checkout:

```bash
uv sync --locked
```

## Setup

Create your own application in the [Clio developer portal](https://developers.clio.com).
Set its redirect URI to `http://127.0.0.1:8765/callback`, then run:

```bash
clio-mcp-setup
```

Enter your application's Client ID and Client Secret (the secret is hidden).
Prefer `CLIO_CLIENT_SECRET` in the process environment when supplying it
non-interactively; no command-line secret argument is accepted. Setup opens your browser
for Clio authorization, receives the callback on the local loopback address,
and stores your credentials and OAuth tokens. Authorize the scopes requested
by setup to use all supported operations. Your account permissions still apply.

For source installs, prefix commands with `uv run`.

Verify the stored connection:

```bash
clio-mcp-verify
```

Verification checks local credentials before calling Clio. With a configured
connection it checks authentication and reads up to five matters without
printing your name or email. Run setup again if reauthorization is required.

## MCP client configuration

Add this entry to your MCP client's configuration:

```json
{
  "mcpServers": {
    "clio": {
      "command": "clio-mcp"
    }
  }
}
```

For a source checkout, use `uv` with an absolute repository path:

```json
{
  "mcpServers": {
    "clio": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/clio-mcp", "run", "clio-mcp"]
    }
  }
}
```

## HTTP mode

Complete the setup above, then select stateless Streamable HTTP. The endpoint
is `http://127.0.0.1:8080/mcp`. Stdio remains the default transport.

> **Security: this endpoint has no authentication and no TLS.** Anyone who can reach the port can run every tool, including write and delete tools, with this server's vendor credentials. Keep the default loopback bind (`127.0.0.1`), or put the server behind an authenticating TLS proxy on a private network. `CLIO_MCP_ALLOWED_HOSTS` and `CLIO_MCP_ALLOWED_ORIGINS` protect against browser DNS rebinding, not against direct callers. A proxy in front of it needs connection and idle timeouts: a legacy-style `GET /mcp` with `Accept: text/event-stream` holds a stream open until the client disconnects.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `CLIO_MCP_TRANSPORT` | `stdio` | Set to `streamable-http` to serve HTTP. |
| `CLIO_MCP_HOST` | `127.0.0.1` | Address to bind. |
| `PORT` | `8080` | Integer listening port. |
| `CLIO_MCP_ALLOWED_HOSTS` | Unset | Required for a non-loopback bind; comma-separated accepted Host values, including ports where needed (`clio.example:8080` or `clio.example:*`). |
| `CLIO_MCP_ALLOWED_ORIGINS` | Unset | Optional comma-separated accepted Origin values (`https://clio.example`); other Origins receive 403. Requests without an Origin are accepted. Loopback binds use the SDK's local Host/Origin allowlist. |
| `CLIO_CLIENT_ID` | Stored credential | Your Clio application's Client ID. |
| `CLIO_CLIENT_SECRET` | Stored credential | Your Clio application's Client Secret. |
| `CLIO_REQUESTS_PER_MINUTE` | `50` | Shared outbound Clio request quota. |
| `CLIO_MCP_USE_KEYRING` | `1` | Set to `0` to use local credential files. |
| `CLIO_MCP_HOME` | `~/.clio-mcp` | Local credential directory. |
| `CLIO_MCP_TOKEN_KEY` | Unset | Optional Fernet key for the local OAuth token cache. |
| `CLIO_MCP_TOKEN_KEY_RECOVERY` | Unset | Existing credential recovery setting; see credential storage below. |
| `CLIO_MCP_FILES_DIR` | `~/clio-mcp-files` | Confined directory for document tools. |
| `CLIO_MCP_MAX_FILE_BYTES` | `104857600` | Maximum document transfer size. |
| `CLIO_MCP_TRANSFER_DEADLINE_SECONDS` | Calculated from size | Optional positive total document transfer deadline. |

```bash
CLIO_MCP_TRANSPORT=streamable-http PORT=8080 uv run clio-mcp
```

HTTP uses the same local credentials and OAuth token storage as stdio;
credentials are never supplied by a request. Host and Origin validation are
enabled. Each protocol 2026-07-28 POST is self-contained, with no initialization
handshake or session ID. The SDK also serves older protocol versions at `/mcp`.
The SDK's SSE response default is retained so a disconnect cancels the request.

## Tools

- `who_am_i`
- `list_practice_areas`
- `list_users`
- `list_matters`
- `get_matter`
- `create_matter`
- `update_matter`
- `list_contacts`
- `get_contact`
- `create_contact`
- `list_tasks`
- `create_task`
- `update_task`
- `complete_task`
- `list_calendar_entries`
- `create_calendar_entry`
- `list_activities`
- `create_time_entry`
- `create_expense_entry`
- `list_bills`
- `list_documents`
- `upload_document`
- `download_document`
- `list_notes`
- `create_note`
- `list_communications`
- `list_timers`
- `start_timer`
- `stop_timer`
- `create_bill`

The four prompts are `daily_briefing`, `prioritize`, `task_report`, and
`strategize`. The resource is `clio://practice_areas`.

Paginated lists stop at five API pages and include narrowing guidance when
truncated. Matter, contact, task, calendar, and activity tools default to a
25-record total limit (1–200); supported sort values are declared in their
input schemas. Other list tools retain their documented page-size semantics.
Document upload and download use **only** `CLIO_MCP_FILES_DIR`, default
`~/clio-mcp-files`. The directory is created with `0700` permissions on first
use; it must be separate from your home root, SSH, AWS, and Clio configuration
directories, including `~/.clio-mcp`. `CLIO_MCP_FILES_DIR` must be absolute.
Existing directory permissions are preserved. Relative tool file paths are relative to this
directory. Move an upload into it yourself. Symlinks, hard-linked uploads,
non-regular files and parent traversal are rejected. Destination parent
directories must already exist. Downloads use a private temporary file and
atomic publication (hard link, or exclusive reservation plus rename where links
are unsupported); `0600` is attempted but mode-less FAT/exFAT/SMB volumes are
allowed for document files. Existing destinations are always refused (the tool has no
overwrite argument). Without `output_path`, a download streams and counts bytes
without saving or buffering the document.

The default transfer cap is 100 MB (104857600 bytes); set the process variable
`CLIO_MCP_MAX_FILE_BYTES` to a positive byte count to change it. HTTPS transfers
use Requests with certifi, `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`, environment
and system proxies, and urllib3's multi-address connection attempts. HTTPS is
required on every hop. Before each direct hop, DNS answers must exclude
loopback, private, link-local, unspecified and multicast addresses. CGNAT
`100.64.0.0/10` and fake-IP `198.18.0.0/15` are allowed for VPN compatibility.
When a proxy is configured for that URL, destination DNS checks are delegated
to the proxy. Redirects are followed manually, revalidated, and limited to
three; PUT redirects must preserve the method (307/308). Storage requests use
an independent session and never receive Clio headers or bearer tokens; implicit
netrc authentication and storage cookies are suppressed.

Transfers stream under the byte cap, a 30-second read inactivity timeout, and a
total caller deadline of `120 seconds + bytes / (128 KiB/second)`, covering DNS,
connect, headers, body and redirects. Uploads use their known size; downloads
budget for the size cap until Content-Length supplies a known size, then use
that size from the original start time. Unknown lengths keep the cap allowance. Override the total
with a positive `CLIO_MCP_TRANSFER_DEADLINE_SECONDS`. A bounded transport worker
lets the caller stop even if Requests is blocked by trickling headers. Cancellation
stops further body processing; an already blocked transport operation may finish
its inactivity timeout in the background. Only the caller writes local files.
Signed URL queries and response bodies never appear in transfer errors.

Setup always asks for Client ID (the stored ID is the default) and a fresh Client
Secret; `--client-id` supplies the ID but still prompts for the secret. The
browser authorization URL is always printed, and the callback window is 600
seconds. If storage PUT fails after document creation, the error identifies the
created Clio document ID so you can delete the empty stub yourself.

## Security model

This is a local stdio server for a trusted MCP client. Writes and file tools are
enabled by default. Tool annotations are hints; the MCP host controls approvals.
Returned Clio record text is untrusted data and must not be treated as instructions.
Ordinary API calls check the credential generation before and after each response
without holding a cross-process lock across the request. A stale response is
refused, but a remote write already sent cannot be undone by local rotation.
Ordinary Clio API requests use per-phase connect/read timeouts that measure
inactivity, so a response that keeps trickling bytes can exceed the nominal
deadline (file transfers have their own total deadline).


## Credential storage

Credentials resolve in this order: OS keyring → process environment →
`~/.clio-mcp/.env`. Setup writes to the OS keyring when available, otherwise to
the local file with `0600` permissions. The keyring service name is `clio-mcp`.

| OS | Native secret store |
| --- | --- |
| macOS | Keychain |
| Windows | Credential Manager |
| Linux | Secret Service / KWallet |

Set the **process environment variable** `CLIO_MCP_USE_KEYRING=0` to use local files. OAuth tokens are cached in the
keyring or `~/.clio-mcp/tokens.json`, with refresh and generation protection.
Legacy local token files are migrated when read. Set the **process environment variable** `CLIO_MCP_TOKEN_KEY` to a
Fernet key to encrypt the file cache; preserve that key to read the cache later.
A mismatched encryption key fails closed. `CLIO_MCP_HOME` can override the
configuration directory. On POSIX it must be on a filesystem that enforces POSIX
permissions (the default `~/.clio-mcp` is); credential storage fails closed on
mode-less volumes. Every read verifies directory `0700`, file `0600`, current
owner, regular-file type and single link; legacy modes are repaired under the
store lock and reverified. Windows uses lstat/type/link/identity checks and
Credential Manager with `msvcrt` locks; POSIX uses no-follow descriptors and
`fcntl` locks. `.env.example` contains only client credential placeholders read from that file.
`CLIO_MCP_USE_KEYRING`, `CLIO_MCP_TOKEN_KEY`, `CLIO_MCP_HOME`,
`CLIO_MCP_TOKEN_KEY_RECOVERY` and file-transfer settings are read from the
process environment, not from `.env`. Without a token key, file tokens are
plaintext protected by directory/file permissions. A keyring write failure
prints a non-secret fallback warning; setup reports the actual backend for the
Client ID, client secret and tokens separately. If a stale keyring entry cannot
be safely removed and verified absent, fallback fails closed. Refresh and
generation writes coordinate across processes through private lock files.
`commit_authorization_grant` persists a pending marker before writing tokens.
If re-setup fails during this commit, both the previous and pending grant remain
unusable until setup completes successfully. This deliberately fails closed;
retry setup after fixing the reported storage problem.

## Development

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python tests/spec_check.py --mcp-only
```

Automated tests use temporary local credentials and fixture responses.
The manual smoke scripts in `tests/` require your own account and are outside
the offline test suite. See the public protocol and migration notes for details.

## License

MIT — Rosen Advertising. See [LICENSE](LICENSE).
