"""Tests for the ApprovalMiddleware decision logic and elicitation flow."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import mcp.types as mt

from mcp_approval_proxy.middleware import (
    ApprovalMiddleware,
    _needs_approval,
    _build_elicitation_message,
    _deny,
)


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _needs_approval
# ─────────────────────────────────────────────────────────────────────────────

class TestNeedsApproval:
    def _ann(self, read_only=False, destructive=False) -> mt.ToolAnnotations:
        return mt.ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive)

    def test_mode_none_always_passes(self):
        assert _needs_approval("delete_file", None, "none", frozenset(), frozenset()) is False

    def test_mode_all_always_gates(self):
        ann = self._ann(read_only=True)  # even read-only
        assert _needs_approval("list_dir", ann, "all", frozenset(), frozenset()) is True

    def test_always_deny_returns_none(self):
        result = _needs_approval("delete_file", None, "destructive", frozenset(), frozenset({"delete_file"}))
        assert result is None

    def test_always_allow_returns_false(self):
        result = _needs_approval("delete_file", None, "all", frozenset({"delete_file"}), frozenset())
        assert result is False

    def test_read_only_hint_skips_approval(self):
        ann = self._ann(read_only=True)
        assert _needs_approval("read_file", ann, "destructive", frozenset(), frozenset()) is False

    def test_destructive_hint_requires_approval(self):
        ann = self._ann(destructive=True)
        assert _needs_approval("some_tool", ann, "destructive", frozenset(), frozenset()) is True

    def test_annotated_mode_only_destructive_hint(self):
        safe = self._ann(read_only=False, destructive=False)
        dest = self._ann(destructive=True)
        assert _needs_approval("write_file", safe, "annotated", frozenset(), frozenset()) is False
        assert _needs_approval("write_file", dest, "annotated", frozenset(), frozenset()) is True

    def test_write_pattern_heuristic(self):
        # These names should trigger write heuristic
        for name in ["write_file", "delete_record", "create_user", "update_config",
                     "remove_entry", "exec_command", "insert_row", "deploy_app"]:
            result = _needs_approval(name, None, "destructive", frozenset(), frozenset())
            assert result is True, f"Expected {name!r} to require approval"

    def test_read_pattern_heuristic_no_approval(self):
        # These names should NOT trigger write heuristic
        for name in ["list_files", "get_record", "fetch_data", "show_logs", "search_index"]:
            ann = self._ann(read_only=False, destructive=False)
            result = _needs_approval(name, ann, "destructive", frozenset(), frozenset())
            assert result is False, f"Expected {name!r} to skip approval"

    def test_no_annotations_write_heuristic(self):
        assert _needs_approval("write_data", None, "destructive", frozenset(), frozenset()) is True

    def test_always_deny_case_insensitive(self):
        result = _needs_approval("DELETE_FILE", None, "destructive", frozenset(), frozenset({"delete_file"}))
        assert result is None

    def test_always_allow_case_insensitive(self):
        result = _needs_approval("READ_FILE", None, "all", frozenset({"read_file"}), frozenset())
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _deny helper
# ─────────────────────────────────────────────────────────────────────────────

class TestDenyHelper:
    def test_returns_error_result(self):
        result = _deny("blocked")
        assert result.isError is True
        assert any("blocked" in c.text for c in result.content if hasattr(c, "text"))


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _build_elicitation_message
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildElicitationMessage:
    def test_includes_server_and_tool(self):
        msg = _build_elicitation_message(
            server_name="filesystem",
            tool_name="write_file",
            tool_args={"path": "/tmp/test.txt"},
            description="Write content to a file",
            annotations=None,
        )
        assert "filesystem" in msg
        assert "write_file" in msg
        assert "/tmp/test.txt" in msg

    def test_destructive_hint_warning(self):
        ann = mt.ToolAnnotations(destructiveHint=True)
        msg = _build_elicitation_message("s", "t", {}, "", ann)
        assert "destructive" in msg.lower()

    def test_long_args_truncated(self):
        big_args = {"data": "x" * 1000}
        msg = _build_elicitation_message("s", "t", big_args, "", None)
        assert len(msg) < 1500  # should be truncated


# ─────────────────────────────────────────────────────────────────────────────
# Integration-style tests: middleware on_call_tool
# ─────────────────────────────────────────────────────────────────────────────

def _make_context(tool_name: str, arguments: dict = None, elicit_result=None, supports_elicitation=True):
    """Build a fake MiddlewareContext for testing."""
    from fastmcp.server.middleware import MiddlewareContext
    from fastmcp.server.elicitation import AcceptedElicitation

    # Build mock FastMCP Context
    fastmcp_ctx = AsyncMock()
    fastmcp_ctx.client_supports_extension = AsyncMock(return_value=supports_elicitation)

    if elicit_result is None:
        elicit_result = AcceptedElicitation(data=True)
    fastmcp_ctx.elicit = AsyncMock(return_value=elicit_result)

    # Build message
    msg = mt.CallToolRequestParams(name=tool_name, arguments=arguments or {})

    return MiddlewareContext(message=msg, fastmcp_context=fastmcp_ctx)


@pytest.mark.asyncio
class TestMiddlewareOnCallTool:

    async def test_read_only_tool_passes_through(self):
        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["list_files"] = mt.Tool(
            name="list_files",
            inputSchema={},
            annotations=mt.ToolAnnotations(readOnlyHint=True),
        )
        ctx = _make_context("list_files")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await middleware.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert result.isError is not True

    async def test_write_tool_approved_passes_through(self):
        from fastmcp.server.elicitation import AcceptedElicitation

        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})

        ctx = _make_context("write_file", elicit_result=AcceptedElicitation(data=True))
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await middleware.on_call_tool(ctx, call_next)
        call_next.assert_awaited_once()

    async def test_write_tool_denied_returns_error(self):
        from fastmcp.server.elicitation import AcceptedElicitation

        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["delete_file"] = mt.Tool(name="delete_file", inputSchema={})

        ctx = _make_context("delete_file", elicit_result=AcceptedElicitation(data=False))
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await middleware.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()
        assert result.isError is True

    async def test_declined_elicitation_returns_error(self):
        from fastmcp.server.elicitation import DeclinedElicitation

        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})

        ctx = _make_context("write_file", elicit_result=DeclinedElicitation())
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)
        assert result.isError is True
        call_next.assert_not_awaited()

    async def test_cancelled_elicitation_returns_error(self):
        from fastmcp.server.elicitation import CancelledElicitation

        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})

        ctx = _make_context("write_file", elicit_result=CancelledElicitation())
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)
        assert result.isError is True

    async def test_always_deny_returns_blocked_error(self):
        middleware = ApprovalMiddleware(
            mode="destructive",
            always_deny=["dangerous_tool"],
            server_name="test",
        )
        ctx = _make_context("dangerous_tool")
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)
        assert result.isError is True
        call_next.assert_not_awaited()

    async def test_always_allow_bypasses_elicitation(self):
        middleware = ApprovalMiddleware(
            mode="all",  # would normally gate everything
            always_allow=["safe_tool"],
            server_name="test",
        )
        ctx = _make_context("safe_tool")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await middleware.on_call_tool(ctx, call_next)
        call_next.assert_awaited_once()
        # Elicitation should NOT have been called
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_mode_none_passes_everything(self):
        middleware = ApprovalMiddleware(mode="none", server_name="test")
        ctx = _make_context("delete_everything")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await middleware.on_call_tool(ctx, call_next)
        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_no_fastmcp_context_returns_error(self):
        from fastmcp.server.middleware import MiddlewareContext

        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})

        msg = mt.CallToolRequestParams(name="write_file", arguments={})
        ctx = MiddlewareContext(message=msg, fastmcp_context=None)
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)
        assert result.isError is True
        call_next.assert_not_awaited()

    async def test_elicitation_not_supported_returns_error(self):
        middleware = ApprovalMiddleware(mode="destructive", server_name="test")
        middleware.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})

        ctx = _make_context("write_file", supports_elicitation=False)
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next)
        assert result.isError is True
        call_next.assert_not_awaited()
