# MCP 2026-07-28 protocol notes

clio-mcp uses the Python MCP SDK 2.2.0 and the stdio transport. The endpoint
registry and all 30 tools, four prompts, and one resource remain registered
through `MCPServer`.

| Protocol area | Handling |
| --- | --- |
| Modern negotiation and discovery | Provided by SDK v2; local regression tests check the negotiated revision. |
| Legacy compatibility | SDK v2 retains the 2025-11-25 client mode. |
| Tool schemas | Existing typed parameters produce object input schemas; limits and sorting are tested. |
| Registry stability | Repeated listings must preserve tool order and the complete registration set. |
| Structured credential errors | Pre-dispatch missing-credential and persisted dead-marker checks return actionable structured results. Exceptions raised inside a tool are masked by SDK 2.2 as `Error executing tool X`; a rejection first discovered during execution uses that SDK behavior. |
| Protocol guard | `python tests/spec_check.py --mcp-only` checks the installed SDK revision without credentials or API calls. |
| Optional features | No application sampling, roots, elicitation, or tracing integration is added. |

The SDK supplies protocol serialization. Application logging uses ordinary
Python stderr logging. Clio OAuth setup is a separate local browser flow using
the user's developer application.

The full offline OpenAPI comparison accepts a user-supplied JSON document:

```bash
python tests/spec_check.py --openapi /path/to/clio-openapi-v4.json
```

It compares that document with `clio_mcp/endpoints.yaml`; it does not fetch the
specification or read credentials. See the
[protocol changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
and [SDK migration guide](https://py.sdk.modelcontextprotocol.io/migration/)
for background.
