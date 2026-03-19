"""
approval-proxy — MCP-native approval proxy

Wraps any MCP server with an approval layer.  When a write/destructive tool is
called, the proxy sends an MCP-native elicitation/create back to the client
(Claude Code, Claude Desktop) — no external polling, no side channels.

Usage:
  approval-proxy --upstream ./mcp.json [options]

Examples:
  # Gate all write tools with elicitation (default)
  approval-proxy --upstream ./mcp.json

  # Use a specific server from Claude Desktop config
  approval-proxy --upstream ~/.claude/claude_desktop_config.json --server filesystem

  # Require approval for every tool call
  approval-proxy --upstream ./mcp.json --mode all

  # Pass-through (no approval) — useful for debugging
  approval-proxy --upstream ./mcp.json --mode none

  # Hard-deny specific tools, always allow others
  approval-proxy --upstream ./mcp.json --deny delete_file --allow read_file,list_dir
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_upstream_config
from .proxy import build_proxy


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="approval-proxy",
        description=(
            "Transparent MCP proxy that gates write/destructive tool calls "
            "behind MCP-native elicitation approval."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument(
        "--upstream", "-u",
        required=True,
        metavar="FILE",
        help="Path to MCP server config JSON (Claude Desktop / claude.json format)",
    )
    p.add_argument(
        "--server", "-s",
        default=None,
        metavar="NAME",
        help="Which server from the config to proxy (default: first server)",
    )
    p.add_argument(
        "--mode", "-m",
        default="destructive",
        choices=["destructive", "all", "annotated", "none"],
        help=(
            "'destructive' (default): gate write-pattern tools + destructiveHint. "
            "'all': gate every tool call. "
            "'annotated': only gate tools with destructiveHint=true. "
            "'none': pass-through, no gating."
        ),
    )
    p.add_argument(
        "--allow",
        default="",
        metavar="tool1,tool2",
        help="Comma-separated tool names that bypass approval",
    )
    p.add_argument(
        "--deny",
        default="",
        metavar="tool1,tool2",
        help="Comma-separated tool names that are always blocked (hard deny)",
    )
    p.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport to expose (default: stdio)",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (for sse/streamable-http)")
    p.add_argument("--port", type=int, default=8765, help="Bind port (for sse/streamable-http)")

    return p.parse_args()


async def _run(args: argparse.Namespace) -> None:
    servers = load_upstream_config(args.upstream)
    if not servers:
        print(f"[approval-proxy] No servers found in {args.upstream}", file=sys.stderr)
        sys.exit(1)

    if args.server:
        matching = [s for s in servers if s.name == args.server]
        if not matching:
            available = ", ".join(s.name for s in servers)
            print(f"[approval-proxy] Server {args.server!r} not found. Available: {available}", file=sys.stderr)
            sys.exit(1)
        server_cfg = matching[0]
    else:
        server_cfg = servers[0]
        if len(servers) > 1:
            print(
                f"[approval-proxy] Multiple servers in config — using {server_cfg.name!r}. "
                f"Use --server to select another.",
                file=sys.stderr,
            )

    always_allow = [t.strip() for t in args.allow.split(",") if t.strip()]
    always_deny = [t.strip() for t in args.deny.split(",") if t.strip()]

    proxy = await build_proxy(
        server_cfg=server_cfg,
        mode=args.mode,
        always_allow=always_allow,
        always_deny=always_deny,
    )

    print(
        f"[approval-proxy] {server_cfg.name!r} | mode={args.mode} | transport={args.transport}",
        file=sys.stderr,
    )

    if args.transport == "stdio":
        proxy.run(transport="stdio")
    elif args.transport == "sse":
        proxy.run(transport="sse", host=args.host, port=args.port)
    elif args.transport == "streamable-http":
        proxy.run(transport="streamable-http", host=args.host, port=args.port)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
