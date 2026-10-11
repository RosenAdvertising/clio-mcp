# Clio MCP server

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP 2026-07-28](https://img.shields.io/badge/MCP-2026--07--28-7C3AED.svg)](https://modelcontextprotocol.io)

Connect Claude and other MCP clients to Clio Manage to manage matters, contacts, tasks, time and billing.

Clio MCP server is a [Model Context Protocol](https://modelcontextprotocol.io) server for [Clio Manage](https://www.clio.com), the legal practice management platform. It registers 30 tools, 4 prompts and 1 resource that read and write Clio data. It runs over stdio by default, for desktop clients such as Claude Desktop, and offers an opt-in stateless Streamable HTTP mode that implements MCP specification 2026-07-28. Clio credentials stay on the machine that runs the server: they come from the setup command and your operating system's keyring, never from the client.

## Features

- **Matters**: list, search, get, create and update matters.
- **Contacts**: search, get and create people and companies.
- **Tasks and calendar**: list, create, update and complete tasks; list and create calendar entries.
- **Time and expenses**: list activities, log billable time and expenses, and start, stop and read a timer.
- **Billing**: list invoices.
- **Documents and notes**: list, upload and download documents; list and create notes; list logged communications.
- **Reference data**: practice areas, firm users and the signed-in Clio user.

## Tools

The server registers 30 tools.

| Tool | What it does |
| --- | --- |
| `who_am_i` | Returns the signed-in Clio user (name, email and ID). |
| `list_practice_areas` | Lists practice areas, up to 5 API pages. |
| `list_users` | Lists firm users, for ID lookup. |
| `list_matters` | Lists matters by status (open, pending or closed), optional text query and sort order. |
| `get_matter` | Returns full matter detail including relationships, custom fields and billing method. |
| `create_matter` | Creates a matter for a client; the practice area is matched by partial name. |
| `update_matter` | Updates a matter's description, status or close date. |
| `list_contacts` | Searches contacts by type (Person or Company) and sort order. |
| `get_contact` | Returns full contact detail including addresses and phone numbers. |
| `create_contact` | Creates a person or a company contact. |
| `list_tasks` | Lists tasks by status (pending or complete), matter and sort order. |
| `create_task` | Creates a task with a priority (Low, Normal or High) and a due date. |
| `update_task` | Updates a task; supply only the fields to change. |
| `complete_task` | Marks a task complete. |
| `list_calendar_entries` | Lists calendar entries in a date range. |
| `create_calendar_entry` | Creates a calendar entry; the calendar owner is resolved from the account when not given. |
| `list_activities` | Lists time entries and expenses, filtered by type, dates and sort order. |
| `create_time_entry` | Logs billable time from decimal hours and an hourly rate. |
| `create_expense_entry` | Logs an expense (disbursement) in dollars. |
| `list_bills` | Lists invoices by status, overdue flag and dates. |
| `list_documents` | Lists documents, filtered by matter or file name. |
| `upload_document` | Uploads a file from the documents directory to Clio. |
| `download_document` | Downloads a document into the documents directory. |
| `list_notes` | Lists notes on a matter. |
| `create_note` | Creates a note on a matter or on a contact. |
| `list_communications` | Lists logged communications, filtered by received date. |
| `list_timers` | Returns the current user's running timer, if one exists. |
| `start_timer` | Starts a billable timer on a matter. |
| `stop_timer` | Stops the running timer. |
| `create_bill` | Returns a `not_supported` status with instructions, because Clio API v4 does not support bill creation. Generate bills in the Clio web interface. |

### Prompts and resources

The server registers four prompts: `daily_briefing`, `prioritize`, `strategize` (takes a matter ID) and `task_report`. It registers one resource, `clio://practice_areas`, which returns the practice areas configured in the Clio account.

## Requirements

- Python 3.10 or later
- A Clio Manage account with API access
- Your own Clio developer application and its Client ID and Client Secret
- An MCP client such as Claude Desktop

## Installation

Install [uv](https://docs.astral.sh/uv/), then clone the repository and install its locked dependencies:

```bash
git clone https://github.com/RosenAdvertising/clio-mcp.git
cd clio-mcp
uv sync --locked
```

## Configuration

Clio uses OAuth, so you create your own application and authorize it once. Create an application in the [Clio developer portal](https://developers.clio.com), set its redirect URI to `http://127.0.0.1:8765/callback`, then run:

```bash
uv run clio-mcp-setup
```

Setup prompts for your application's Client ID (the stored ID is the default; `--client-id` supplies it) and always prompts for a fresh Client Secret, which is hidden; no command-line secret argument is accepted. It prints the Clio authorization URL, opens your browser, receives the callback on the local loopback address for up to 600 seconds, and stores your credentials and OAuth tokens (see [Credential storage](#credential-storage)). Authorize the scopes requested by setup to use all supported operations. Your account permissions still apply.

Verify the stored connection:

```bash
uv run clio-mcp-verify
```

Verification checks local credentials before calling Clio. With a configured connection it checks authentication and reads up to five matters without printing your name or email. Run setup again if reauthorization is required.

All variables are read from the process environment. `CLIO_CLIENT_ID` and `CLIO_CLIENT_SECRET` resolve from the OS keyring first, then the process environment, then `~/.clio-mcp/.env`.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `CLIO_CLIENT_ID` | Only if not stored by setup | stored credential | Your Clio application's Client ID. |
| `CLIO_CLIENT_SECRET` | Only if not stored by setup | stored credential | Your Clio application's Client Secret. |
| `CLIO_REQUESTS_PER_MINUTE` | No | `50` | Outbound Clio request limit per minute, shared by all calls. Invalid or non-positive values fall back to 50. |
| `CLIO_MCP_USE_KEYRING` | No | `1` | Set to `0` (or `false`, `no`, `off`) to use local credential files instead of the OS keyring. |
| `CLIO_MCP_HOME` | No | `~/.clio-mcp` | Directory for the local credential files. |
| `CLIO_MCP_TOKEN_KEY` | No | unset | Fernet key that encrypts the local OAuth token cache. Preserve it to read the cache later. |
| `CLIO_MCP_TOKEN_KEY_RECOVERY` | No | unset | Set to `1` to set aside an encrypted token cache that cannot be decrypted with the current key, so setup can be run again. |
| `CLIO_MCP_FILES_DIR` | No | `~/clio-mcp-files` | Absolute path of the directory the document tools read and write. |
| `CLIO_MCP_MAX_FILE_BYTES` | No | `104857600` | Maximum document transfer size in bytes. Must be a positive integer. |
| `CLIO_MCP_TRANSFER_DEADLINE_SECONDS` | No | calculated from size | Total deadline for one document transfer, in seconds. Must be a positive number. |

## Usage with Claude Desktop

Add the server to Claude Desktop's configuration file (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "clio": {
      "command": "uv",
      "args": ["run", "--locked", "--directory", "/absolute/path/to/clio-mcp", "clio-mcp"]
    }
  }
}
```

Replace `/absolute/path/to/clio-mcp` with the path of your clone, then restart Claude Desktop. Any other stdio MCP client uses the same command and arguments. Run `uv run clio-mcp-setup` first: the server reads the credentials that setup stores.

## HTTP mode

Stdio is the default. Set `CLIO_MCP_TRANSPORT=streamable-http` to serve the stateless Streamable HTTP transport from MCP specification 2026-07-28 at `/mcp`. Each request stands alone: no initialization handshake and no `Mcp-Session-Id`. Clients on earlier protocol versions are served on the same endpoint. Complete [Configuration](#configuration) first: HTTP mode uses the same stored credentials.

> **Security: this endpoint has no authentication and no TLS.** Anyone who can reach the port can run every tool, including write and delete tools, with this server's vendor credentials. Keep the default loopback bind (`127.0.0.1`), or put the server behind an authenticating TLS proxy on a private network. `CLIO_MCP_ALLOWED_HOSTS` and `CLIO_MCP_ALLOWED_ORIGINS` protect against browser DNS rebinding, not against direct callers. A proxy in front of it needs connection and idle timeouts: a legacy-style `GET /mcp` with `Accept: text/event-stream` holds a stream open until the client disconnects.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLIO_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` (case-insensitive; an empty value selects `stdio`). |
| `CLIO_MCP_HOST` | `127.0.0.1` | Bind address. `127.0.0.1`, `localhost` and `::1` use the SDK's built-in Host and Origin checks; any other value requires `CLIO_MCP_ALLOWED_HOSTS`. Values are compared exactly, so `LOCALHOST` counts as non-loopback. |
| `PORT` | `8080` | Port; must be an integer. |
| `CLIO_MCP_ALLOWED_HOSTS` | unset | Comma-separated `Host` header values accepted on a non-loopback bind, such as `clio.example:8080` or `clio.example:*`. |
| `CLIO_MCP_ALLOWED_ORIGINS` | unset | Comma-separated `Origin` values accepted on a non-loopback bind, such as `https://clio.example`. Requests with any other `Origin` receive 403; requests without an `Origin` header are accepted. |

Clio credentials come from the same local storage as stdio (see [Configuration](#configuration)), never from the request. A client disconnect cancels the running request.

```bash
CLIO_MCP_TRANSPORT=streamable-http PORT=8080 uv run --locked clio-mcp
```

Point the MCP client at `http://127.0.0.1:8080/mcp`.

## Error handling

- Credential problems are detected before any Clio call. If setup has not been run, the tool returns a JSON error with `"code": "clio_connection_missing"` and the message `No Clio credentials are configured. Run clio-mcp-setup.` Once Clio has permanently rejected the stored authorization, later calls return `"code": "clio_reauthorization_required"` with a message to run `clio-mcp-setup` again. The server itself starts without credentials.
- When Clio answers a record tool with an error status (for example 404 or 422), the tool returns Clio's own JSON error body as an ordinary result, not flagged `isError`. A response that is not JSON is reported only as `Error executing tool <tool>`.
- The document tools (`upload_document` and `download_document`) catch their own failures and return `{"error": "<message>"}` with a fixed message: path rejections, transfer failures and Clio document errors never include URLs, query strings or response bodies. These are ordinary results, not flagged `isError`.
- Clio `429` responses are retried up to three times, waiting for the `Retry-After` value (10 seconds when absent, at most 30); if the limit persists the call fails. A `401` triggers one token refresh and one retry. `5xx` responses are not retried.
- Outbound requests share a rolling limit of 50 per minute (`CLIO_REQUESTS_PER_MINUTE`). A call waits for a free slot and fails if the wait would pass its deadline.
- Each Clio request has a 10-second connect timeout and a 30-second read timeout, measured as inactivity, inside a 90-second deadline for the request.
- A failure raised while a tool runs, such as a timeout, a failed connection, a persistent rate limit or a credential change during the request, reaches the client only as `Error executing tool <tool>`; the cause is written to the server log.
- An unsupported `CLIO_MCP_TRANSPORT`, a non-integer `PORT`, or a non-loopback `CLIO_MCP_HOST` without `CLIO_MCP_ALLOWED_HOSTS` exits with code 1 and a message naming the variable.

## Pagination and result limits

Paginated lists stop at five API pages and return `truncated: true` with narrowing guidance when more records exist. Matter, contact, task, calendar and activity tools default to a 25-record total limit (1 to 200); supported sort values are declared in their input schemas. Other list tools retain their documented page-size semantics.

## Document files and transfers

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

If the storage upload fails after Clio has created the document, the error identifies the created Clio document ID so you can delete the empty document yourself.

## Security model

This is a local server for a trusted MCP client. Writes and file tools are
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
Setup records a pending marker before it writes tokens. If re-running setup fails at that step, both the previous and the pending authorization stay unusable until setup completes successfully. This deliberately fails closed; retry setup after fixing the reported storage problem.

## Testing

The test suite runs offline and needs no Clio account: it substitutes fixture responses for every Clio API call, uses temporary local credential storage and refuses outbound network connections. It covers each tool's requests and responses, list limits and sort orders, error handling, retries and request deadlines, credential storage and rotation, document file and transfer safety, the OAuth setup flow, the stdio server, and the Streamable HTTP transport including the 2026-07-28 wire format, Host and Origin checks and stateless requests.

```bash
uv sync --locked
uv run --locked pytest -q
```

CI runs the suite on every push and pull request to `main`.

The manual smoke scripts in `tests/` require your own Clio account and are outside the offline test suite.

## License

MIT. See [LICENSE](LICENSE).
