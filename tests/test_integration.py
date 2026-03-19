"""Integration test: approval proxy wrapping a real FastMCP server.

Uses an in-process FastMCP server (no subprocess) to verify the full
middleware→elicitation→approve/deny flow end-to-end.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from fastmcp import FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from fastmcp.client import Client
import mcp.types as mt

from mcp_approval_proxy.middleware import ApprovalMiddleware


# ─────────────────────────────────────────────────────────────────────────────
# Shared test server
# ─────────────────────────────────────────────────────────────────────────────

def make_test_server() -> FastMCP:
    """A minimal FastMCP server with one read and one write tool."""
    server = FastMCP(name="test-upstream")

    @server.tool(description="Read a file")
    def read_file(path: str) -> str:
        return f"contents of {path}"

    @server.tool(description="Write content to a file")
    def write_file(path: str, content: str) -> str:
        return f"wrote {len(content)} bytes to {path}"

    @server.tool(description="Delete a file permanently")
    def delete_file(path: str) -> str:
        return f"deleted {path}"

    return server


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_fastmcp_ctx(approved: bool = True, elicit_result=None, supports_elicitation: bool = True):
    """Mock FastMCP Context object."""
    if elicit_result is None:
        elicit_result = AcceptedElicitation(data=approved)
    ctx = AsyncMock()
    ctx.client_supports_extension = AsyncMock(return_value=supports_elicitation)
    ctx.elicit = AsyncMock(return_value=elicit_result)
    return ctx


def _make_middleware_context(tool_name: str, args: dict = None, fastmcp_ctx=None):
    from fastmcp.server.middleware import MiddlewareContext
    msg = mt.CallToolRequestParams(name=tool_name, arguments=args or {})
    return MiddlewareContext(message=msg, fastmcp_context=fastmcp_ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestApprovalProxyWithFastMCPServer:
    """Test middleware against real tool registry from a FastMCP server."""

    async def _build_middleware_with_real_tools(self, mode="destructive"):
        """Populate tool registry from a real FastMCP server."""
        server = make_test_server()
        client = Client(server)
        async with client:
            tools = await client.list_tools()

        middleware = ApprovalMiddleware(mode=mode, server_name="test-upstream")
        middleware.tool_registry = {t.name: t for t in tools}
        return middleware

    async def test_read_file_passes_without_approval(self):
        middleware = await self._build_middleware_with_real_tools()
        ctx = _make_middleware_context("read_file", {"path": "/tmp/foo"}, _make_fastmcp_ctx())
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[
            mt.TextContent(type="text", text="file contents")
        ]))

        result = await middleware.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        # read_file should not trigger elicitation
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_write_file_requires_approval_and_passes_when_approved(self):
        middleware = await self._build_middleware_with_real_tools()
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_middleware_context("write_file", {"path": "/tmp/x", "content": "hi"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[
            mt.TextContent(type="text", text="wrote 2 bytes")
        ]))

        result = await middleware.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_awaited_once()
        call_next.assert_awaited_once()
        assert not result.isError

    async def test_write_file_denied_returns_error(self):
        middleware = await self._build_middleware_with_real_tools()
        fastmcp_ctx = _make_fastmcp_ctx(approved=False)
        ctx = _make_middleware_context("write_file", {"path": "/etc/passwd", "content": "evil"}, fastmcp_ctx)
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_awaited_once()
        call_next.assert_not_awaited()
        assert result.isError is True

    async def test_delete_file_requires_approval(self):
        middleware = await self._build_middleware_with_real_tools()
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_middleware_context("delete_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await middleware.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_awaited_once()

    async def test_mode_all_gates_read_file_too(self):
        middleware = await self._build_middleware_with_real_tools(mode="all")
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_middleware_context("read_file", {"path": "/tmp/foo"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await middleware.on_call_tool(ctx, call_next)

        # mode=all should gate even read_file
        fastmcp_ctx.elicit.assert_awaited_once()

    async def test_mode_none_passes_everything(self):
        middleware = await self._build_middleware_with_real_tools(mode="none")
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_middleware_context("delete_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await middleware.on_call_tool(ctx, call_next)

        # mode=none should never elicit
        fastmcp_ctx.elicit.assert_not_awaited()
        call_next.assert_awaited_once()

    async def test_declined_elicitation_blocks_call(self):
        middleware = await self._build_middleware_with_real_tools()
        fastmcp_ctx = _make_fastmcp_ctx(elicit_result=DeclinedElicitation())
        ctx = _make_middleware_context("write_file", {"path": "/tmp/x", "content": "x"}, fastmcp_ctx)
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)

        assert result.isError is True
        call_next.assert_not_awaited()

    async def test_always_deny_blocks_without_elicitation(self):
        middleware = await self._build_middleware_with_real_tools()
        middleware.always_deny = frozenset({"delete_file"})
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_middleware_context("delete_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)

        # Hard deny — no elicitation, just blocked
        fastmcp_ctx.elicit.assert_not_awaited()
        assert result.isError is True

    async def test_always_allow_bypasses_elicitation(self):
        middleware = await self._build_middleware_with_real_tools(mode="all")
        middleware.always_allow = frozenset({"write_file"})
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_middleware_context("write_file", {"path": "/tmp/x", "content": "x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await middleware.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_not_awaited()
        call_next.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# Test proxy list_tools passthrough
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proxy_exposes_upstream_tools():
    """The proxy should expose the same tools as the upstream server."""
    server = make_test_server()
    client = Client(server)

    proxy = create_proxy(client)
    middleware = ApprovalMiddleware(mode="destructive", server_name="test")

    async with client:
        tools = await client.list_tools()
    middleware.tool_registry = {t.name: t for t in tools}
    proxy.add_middleware(middleware)

    # Connect to the proxy and verify tools are exposed
    proxy_client = Client(proxy)
    async with proxy_client:
        proxy_tools = await proxy_client.list_tools()

    tool_names = {t.name for t in proxy_tools}
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "delete_file" in tool_names
