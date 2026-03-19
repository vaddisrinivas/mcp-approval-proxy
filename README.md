# mcp-approval-proxy

A transparent MCP proxy that intercepts write/destructive tool calls and gates them behind **MCP-native elicitation approval** — no side channels, no webhooks, no polling.

When a guarded tool is called, the proxy sends an `elicitation/create` request back to the MCP client (Claude Code, Claude Desktop). The user sees a native inline approval dialog. If they approve, the call is forwarded to the upstream server. If they decline, the call is blocked with an error.

## How it works

```
Claude Code  ──stdio──▶  approval-proxy  ──stdio──▶  upstream MCP server
                              │
                    elicitation/create ◀── proxy sends approval request
                              │
                    user approves / denies in Claude Code
```

## Install

```bash
pip install mcp-approval-proxy
# or
uvx mcp-approval-proxy
```

## Usage

```bash
# Gate all write tools on a server from Claude Desktop config
approval-proxy --upstream ~/.claude/claude_desktop_config.json --server filesystem

# Gate every tool call (not just writes)
approval-proxy --upstream ./mcp.json --mode all

# Pass-through (no gating — useful for debugging)
approval-proxy --upstream ./mcp.json --mode none

# Hard-deny specific tools, always allow others
approval-proxy --upstream ./mcp.json \
  --deny delete_file \
  --allow read_file,list_dir
```

## Upstream config format

Supports Claude Desktop / `claude.json` format:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "approvalRules": {
        "mode": "destructive",
        "alwaysAllow": ["read_file", "list_dir"],
        "alwaysDeny": ["delete_file"]
      }
    }
  }
}
```

## Approval modes

| Mode | Behaviour |
|------|-----------|
| `destructive` (default) | Gate tools with `destructiveHint=true` or write-like names |
| `all` | Gate every tool call |
| `annotated` | Only gate tools explicitly marked `destructiveHint=true` |
| `none` | Pass-through — no gating |

## Use as a library

```python
from fastmcp.server import create_proxy
from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport
from mcp_approval_proxy.middleware import ApprovalMiddleware

transport = StdioTransport("npx", ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
client = Client(transport)
proxy = create_proxy(client)

middleware = ApprovalMiddleware(mode="destructive", server_name="filesystem")
proxy.add_middleware(middleware)

proxy.run(transport="stdio")
```

## Add to Claude Code / MCP config

```json
{
  "mcpServers": {
    "filesystem-guarded": {
      "command": "approval-proxy",
      "args": [
        "--upstream", "/path/to/filesystem-config.json",
        "--mode", "destructive"
      ]
    }
  }
}
```
