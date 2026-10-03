# MCP SDK migration notes

The package retains its MCP SDK 2 migration and pins `mcp==2.2.0`. Server
construction uses `MCPServer`; the local credential-aware `call_tool` override
forwards the optional context. Python model access uses the SDK v2 snake-case
fields while wire serialization remains SDK-owned.

The public entry point calls `mcp.run()` over stdio. Local OAuth setup and
credential verification are separate commands. The tool set remains 30 tools,
four prompts, and one `clio://practice_areas` resource. Bounded pagination,
record limits, supported sort orders, and existing Clio payload construction
remain covered by offline regression tests.

`tests/test_spec_2026_07_28.py` covers the protocol guard, modern and legacy local
client negotiation, deterministic registrations, and structured missing-token
errors. `tests/spec_check.py --mcp-only` runs independently of credentials.
The optional OpenAPI check uses a supplied local JSON specification.

Ordinary tool exceptions retain the SDK's default error handling. The request
client's deadline helpers split connect/read allowances and refuse calls below
the minimum budget. A Requests read timeout measures inactivity rather than a
strict absolute deadline against continuously arriving bytes.

Run the public checks from the repository root:

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python tests/spec_check.py --mcp-only
```

See [protocol notes](SPEC-DELTA-2026-07-28.md) for the public scope. These notes
describe the source and regression coverage; they do not certify a live Clio
account or vendor response.
