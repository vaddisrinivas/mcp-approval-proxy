"""Build and run the approval proxy for a single upstream server."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastmcp.server import create_proxy
from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport

from .config import ServerConfig
from .middleware import ApprovalMiddleware


async def build_proxy(
    server_cfg: ServerConfig,
    mode: str,
    always_allow: list[str],
    always_deny: list[str],
):
    """
    Connect to upstream MCP server, pre-fetch tool list for annotation cache,
    attach approval middleware, return a ready FastMCPProxy.
    """
    env = {**os.environ, **server_cfg.env}

    transport = StdioTransport(
        command=server_cfg.command,
        args=server_cfg.args,
        env=env,
    )
    client = Client(transport)

    # Effective settings (server-level config overrides global)
    effective_mode = server_cfg.mode or mode
    effective_allow = list(always_allow) + list(server_cfg.always_allow)
    effective_deny = list(always_deny) + list(server_cfg.always_deny)

    middleware = ApprovalMiddleware(
        mode=effective_mode,
        always_allow=effective_allow,
        always_deny=effective_deny,
        server_name=server_cfg.name,
    )

    proxy = create_proxy(client, name=f"approval-proxy/{server_cfg.name}")

    # Wrap lifespan to pre-fetch tool list once at startup
    original_lifespan = proxy.lifespan

    @asynccontextmanager
    async def _augmented_lifespan(server):
        async with original_lifespan(server):
            try:
                async with client:
                    tools = await client.list_tools()
                    middleware.tool_registry = {t.name: t for t in tools}
                    print(
                        f"[approval-proxy] {server_cfg.name!r}: "
                        f"{len(tools)} tool(s) indexed (mode={effective_mode})",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"[approval-proxy] Warning: could not pre-fetch tools: {exc}",
                    file=sys.stderr,
                )
            yield

    proxy.lifespan = _augmented_lifespan
    proxy.add_middleware(middleware)
    return proxy
