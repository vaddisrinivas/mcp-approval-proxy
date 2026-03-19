"""Tests for config loading."""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from mcp_approval_proxy.config import load_upstream_config, ServerConfig


@pytest.fixture
def tmp_config(tmp_path):
    """Helper to write a config JSON and return the path."""
    def _write(data: dict | list) -> Path:
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(data))
        return p
    return _write


class TestLoadUpstreamConfig:

    def test_claude_desktop_format(self, tmp_config):
        p = tmp_config({
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                },
                "sqlite": {
                    "command": "uvx",
                    "args": ["mcp-server-sqlite", "--db-path", "/tmp/test.db"],
                },
            }
        })
        servers = load_upstream_config(p)
        assert len(servers) == 2
        assert servers[0].name == "filesystem"
        assert servers[0].command == "npx"
        assert servers[0].args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        assert servers[1].name == "sqlite"

    def test_single_server_format(self, tmp_config):
        p = tmp_config({"command": "npx", "args": ["-y", "some-mcp-server"]})
        servers = load_upstream_config(p)
        assert len(servers) == 1
        assert servers[0].name == "upstream"
        assert servers[0].command == "npx"

    def test_array_format(self, tmp_config):
        p = tmp_config([
            {"name": "a", "command": "cmd_a", "args": []},
            {"name": "b", "command": "cmd_b", "args": ["--flag"]},
        ])
        servers = load_upstream_config(p)
        assert len(servers) == 2
        assert servers[0].name == "a"
        assert servers[1].name == "b"
        assert servers[1].args == ["--flag"]

    def test_env_vars_included(self, tmp_config):
        p = tmp_config({
            "mcpServers": {
                "myserver": {
                    "command": "npx",
                    "args": [],
                    "env": {"MY_KEY": "my_value", "DEBUG": "1"},
                }
            }
        })
        servers = load_upstream_config(p)
        assert servers[0].env == {"MY_KEY": "my_value", "DEBUG": "1"}

    def test_approval_rules_parsed(self, tmp_config):
        p = tmp_config({
            "mcpServers": {
                "fs": {
                    "command": "npx",
                    "args": [],
                    "approvalRules": {
                        "mode": "annotated",
                        "alwaysAllow": ["read_file", "list_dir"],
                        "alwaysDeny": ["delete_file"],
                    }
                }
            }
        })
        servers = load_upstream_config(p)
        s = servers[0]
        assert s.mode == "annotated"
        assert "read_file" in s.always_allow
        assert "list_dir" in s.always_allow
        assert "delete_file" in s.always_deny

    def test_missing_command_raises(self, tmp_config):
        p = tmp_config({"not": "valid"})
        with pytest.raises(ValueError, match="Unrecognised config format"):
            load_upstream_config(p)

    def test_empty_env_defaults_to_dict(self, tmp_config):
        p = tmp_config({"command": "myserver", "args": []})
        servers = load_upstream_config(p)
        assert servers[0].env == {}

    def test_always_allow_lowercased(self, tmp_config):
        p = tmp_config({
            "command": "x",
            "args": [],
            "approvalRules": {"alwaysAllow": ["ReadFile", "ListDir"]},
        })
        servers = load_upstream_config(p)
        assert "readfile" in servers[0].always_allow
        assert "listdir" in servers[0].always_allow
