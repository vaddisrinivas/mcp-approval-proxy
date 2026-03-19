"""FastMCP middleware that intercepts tool calls and gates them behind approval.

Architecture:
  MCP Client (Claude Code)
       │  stdio / SSE
       ▼
  ApprovalProxy (this file — FastMCP middleware)
       │  on_call_tool() intercepts ALL tool calls
       │  ├── read-only / always-allow  →  forward immediately
       │  └── write / destructive       →  fire approval channel
       │            ├── approved  →  forward to upstream via call_next
       │            └── denied    →  return error CallToolResult
       ▼
  Upstream MCP Server (subprocess)
"""

from __future__ import annotations

import re
import sys
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation, CancelledElicitation

# ── Write-pattern heuristic ───────────────────────────────────────────────────
# Exact words (matched after splitting snake_case / camelCase / kebab-case)
_WRITE_WORDS = frozenset({
    "write", "create", "update", "delete", "remove", "move", "rename",
    "insert", "append", "set", "put", "post", "patch", "execute", "exec",
    "run", "trash", "kill", "drop", "truncate", "clear", "reset", "destroy",
    "overwrite", "replace", "modify", "edit", "push", "deploy", "upload",
    "import", "send", "publish", "commit", "merge", "checkout", "tag",
    "release", "rollback", "restore", "wipe", "purge", "format", "mount",
    "enable", "disable", "start", "stop", "restart", "terminate", "shutdown",
    "install", "uninstall", "add", "save", "store", "submit",
})

# Split snake_case, kebab-case, camelCase, and PascalCase into words
_SPLIT_RE = re.compile(
    r"[_\-\s]"              # snake / kebab / space
    r"|(?<=[a-z])(?=[A-Z])"  # camelCase: e → C
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # ABCDef → ABC / Def
)


def _is_write_heuristic(tool_name: str) -> bool:
    """Return True if the tool name contains a write-like word segment."""
    words = _SPLIT_RE.split(tool_name)
    return any(w.lower() in _WRITE_WORDS for w in words if w)


def _needs_approval(
    tool_name: str,
    annotations: mt.ToolAnnotations | None,
    mode: str,
    always_allow: frozenset[str],
    always_deny: frozenset[str],
) -> bool | None:
    """
    Returns:
        None  → hard block (always_deny)
        False → skip approval (always_allow or read-only)
        True  → request approval
    """
    lname = tool_name.lower()

    if lname in always_deny:
        return None  # hard block

    if lname in always_allow:
        return False  # pass through

    if mode == "none":
        return False  # passthrough mode

    if mode == "all":
        return True

    read_only = annotations.readOnlyHint if annotations else False
    destructive = annotations.destructiveHint if annotations else False

    if mode == "annotated":
        return destructive if destructive else False

    # mode == "destructive" (default)
    if read_only:
        return False  # explicitly safe

    if destructive:
        return True   # explicitly destructive

    # Heuristic: tool name contains a write-like word segment
    return _is_write_heuristic(tool_name)


def _deny(message: str) -> mt.CallToolResult:
    return mt.CallToolResult(
        content=[mt.TextContent(type="text", text=message)],
        isError=True,
    )


class ApprovalMiddleware(Middleware):
    """
    Intercepts `call_tool` requests, requests MCP-native approval via
    `elicitation/create`, and either forwards or denies the call.
    """

    def __init__(
        self,
        mode: str = "destructive",
        always_allow: list[str] | None = None,
        always_deny: list[str] | None = None,
        server_name: str = "upstream",
    ):
        self.mode = mode
        self.always_allow = frozenset(t.lower() for t in (always_allow or []))
        self.always_deny = frozenset(t.lower() for t in (always_deny or []))
        self.server_name = server_name
        # Populated after the proxy connects: {tool_name: mt.Tool}
        self.tool_registry: dict[str, mt.Tool] = {}

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next,
    ) -> mt.CallToolResult:
        tool_name: str = context.message.name
        tool_args: dict = context.message.arguments or {}

        tool = self.tool_registry.get(tool_name)
        annotations = tool.annotations if tool else None
        description = (tool.description or "") if tool else ""

        decision = _needs_approval(
            tool_name, annotations, self.mode, self.always_allow, self.always_deny
        )

        if decision is None:
            return _deny(f"⛔ Tool `{tool_name}` is blocked by policy.")

        if not decision:
            return await call_next(context)

        # ── Elicitation approval ──────────────────────────────────────────────
        ctx = context.fastmcp_context
        if ctx is None:
            # No FastMCP context — deny with explanation
            return _deny(
                f"❌ Cannot request approval for `{tool_name}` "
                f"(no client context). Call denied."
            )

        # Check if the MCP client supports elicitation
        if not await _client_supports_elicitation(ctx):
            # Fall back: approve with a warning so we don't silently block
            print(
                f"[approval-proxy] Client does not support elicitation — "
                f"auto-denying `{tool_name}`",
                file=sys.stderr,
            )
            return _deny(
                f"❌ Approval required for `{tool_name}` but client does not "
                f"support elicitation/create. Denied."
            )

        message = _build_elicitation_message(
            server_name=self.server_name,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description,
            annotations=annotations,
        )

        try:
            result = await ctx.elicit(message, response_type=bool)
        except Exception as exc:
            print(f"[approval-proxy] elicitation error for `{tool_name}`: {exc}", file=sys.stderr)
            return _deny(f"❌ Elicitation failed for `{tool_name}`: {exc}")

        if isinstance(result, AcceptedElicitation):
            if result.data:
                return await call_next(context)
            else:
                return _deny(f"❌ Tool call `{tool_name}` denied by user.")
        elif isinstance(result, DeclinedElicitation):
            return _deny(f"❌ Tool call `{tool_name}` declined.")
        else:  # CancelledElicitation
            return _deny(f"❌ Tool call `{tool_name}` cancelled.")


async def _client_supports_elicitation(ctx: Any) -> bool:
    try:
        return await ctx.client_supports_extension("elicitation")
    except Exception:
        pass
    try:
        caps = ctx.session.client_params.capabilities
        return caps is not None and getattr(caps, "elicitation", None) is not None
    except Exception:
        return False


def _build_elicitation_message(
    server_name: str,
    tool_name: str,
    tool_args: dict,
    description: str,
    annotations: mt.ToolAnnotations | None,
) -> str:
    import json

    lines = [
        f"🔐 **Approval required**",
        f"",
        f"**Server:** `{server_name}`  |  **Tool:** `{tool_name}`",
    ]
    if description:
        lines.append(f"*{description}*")
    if annotations and annotations.destructiveHint:
        lines.append(f"⚠️ Marked **destructive** by server")
    if tool_args:
        pretty = json.dumps(tool_args, indent=2, ensure_ascii=False)
        if len(pretty) > 600:
            pretty = pretty[:600] + "\n..."
        lines.append(f"\n**Arguments:**\n```json\n{pretty}\n```")
    lines.append(f"\nAllow this tool call?")
    return "\n".join(lines)
