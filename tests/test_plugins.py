"""Plugins config parse + MCP door fails closed for unknown servers."""

from pathlib import Path

import pytest

from orbit_common.mcp_door import McpDoor, StdioMcpServer, check_mcp
from orbit_common.plugins import load_plugins_config, parse_plugins
from orbit_common.skills import roots_from_setting


def test_parse_plugins_defaults_and_stdio_argv():
    cfg = parse_plugins({
        "skill_roots": ["./skills", "home"],
        "mcp_servers": {
            "notes": {"command": ["npx", "-y", "mcp-notes"], "env": {"A": "1"}},
        },
    })
    assert cfg.skill_roots == ("./skills", "home")
    assert cfg.mcp_servers["notes"].command == ("npx", "-y", "mcp-notes")
    assert cfg.mcp_servers["notes"].env == {"A": "1"}
    assert cfg.mcp_servers["notes"].url is None


def test_parse_plugins_command_plus_args_and_url():
    stdio = parse_plugins({"mcp_servers": {"cal": {"command": "npx", "args": ["-y", "cal"]}}})
    assert stdio.mcp_servers["cal"].command == ("npx", "-y", "cal")
    remote = parse_plugins({"mcp_servers": {"search": {"url": "https://mcp.example.com/rpc"}}})
    assert remote.mcp_servers["search"].url == "https://mcp.example.com/rpc"
    assert remote.mcp_servers["search"].command is None


def test_parse_plugins_refuses_shell_and_bad_shapes():
    with pytest.raises(ValueError):
        parse_plugins({"mcp_servers": {"x": {"command": "echo hi"}}})
    with pytest.raises(ValueError):
        parse_plugins({"mcp_servers": {"x": {"command": ["bash", "-c", "id"]}}})
    with pytest.raises(ValueError):
        parse_plugins({"mcp_servers": {"Bad": {"command": ["npx"]}}})
    with pytest.raises(ValueError):
        parse_plugins({"mcp_servers": {"x": {"command": ["npx"], "url": "https://example.com"}}})
    with pytest.raises(ValueError):
        parse_plugins({"mcp_servers": {"x": {"url": "http://evil.example"}}})
    with pytest.raises(ValueError):
        parse_plugins({"extra": 1})


def test_load_plugins_config_missing_file_is_empty(tmp_path):
    cfg = load_plugins_config(tmp_path / "missing.yaml")
    assert cfg.mcp_servers == {}
    assert cfg.skill_roots == ("./skills",)


def test_load_shipped_plugins_yaml():
    root = Path(__file__).resolve().parents[1]
    cfg = load_plugins_config(root / "config" / "orbit.plugins.yaml")
    assert cfg.mcp_servers == {}
    assert "./skills" in cfg.skill_roots


async def test_mcp_door_refuses_unknown_server():
    door = McpDoor({})
    with pytest.raises(RuntimeError, match="not configured"):
        await door({"type": "mcp_read", "params": {"server": "notes", "tool": "list", "arguments": {}}})


async def test_mcp_door_from_plugins_empty(tmp_path):
    path = tmp_path / "orbit.plugins.yaml"
    path.write_text("skill_roots: [./skills]\nmcp_servers: {}\n")
    door = McpDoor.from_plugins(path)
    assert door.servers == {}
    with pytest.raises(RuntimeError, match="not configured"):
        await door({"type": "mcp_call", "params": {
            "server": "notes", "tool": "create", "arguments": {"title": "x"}}})


def test_stdio_server_rejects_shell_string():
    with pytest.raises(ValueError):
        StdioMcpServer("echo hi")  # type: ignore[arg-type]


def test_skill_roots_expand_home_token():
    roots = roots_from_setting("./skills,home")
    assert roots[0] == Path("./skills")
    assert any("skills" in str(path) or "plugins" in str(path) for path in roots[1:])


def test_check_mcp_still_validates():
    assert check_mcp({"type": "mcp_read", "params": {
        "server": "notes", "tool": "list", "arguments": {}}})["server"] == "notes"
