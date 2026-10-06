"""Orbit plugins config: MCP servers. Fail closed. No shell string. Skill roots use ORBIT_SKILL_ROOTS."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import yaml

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DEFAULT_PATH = Path("config/orbit.plugins.yaml")


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: tuple[str, ...] | None = None
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginsConfig:
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)


def default_plugins_path() -> Path:
    return Path((__import__("os").environ.get("ORBIT_PLUGINS_CONFIG") or "").strip() or _DEFAULT_PATH)


def load_plugins_config(path: Path | str | None = None) -> PluginsConfig:
    target = Path(path) if path is not None else default_plugins_path()
    if not target.is_file():
        return PluginsConfig()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"plugins config unreadable: {target}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError("plugins config is not valid YAML") from error
    return parse_plugins(data)


def parse_plugins(data) -> PluginsConfig:
    if data is None:
        return PluginsConfig()
    if not isinstance(data, dict):
        raise ValueError("plugins config must be a mapping")
    unknown = set(data) - {"mcp_servers"}
    if unknown:
        raise ValueError("plugins config has an unknown field")
    servers = _mcp_servers(data.get("mcp_servers", {}))
    return PluginsConfig(mcp_servers=servers)


def _mcp_servers(raw) -> dict[str, McpServerConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("mcp_servers must be a mapping")
    servers = {}
    for name, body in raw.items():
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise ValueError("MCP server name is invalid")
        if not isinstance(body, dict):
            raise ValueError("MCP server entry must be a mapping")
        unknown = set(body) - {"command", "args", "url", "env"}
        if unknown:
            raise ValueError("MCP server has an unknown field")
        command = _command(body)
        url = body.get("url")
        if url is not None:
            if not isinstance(url, str) or not url.strip() or len(url) > 300:
                raise ValueError("MCP server url must be a short string")
            url = url.strip()
            if not (url.startswith("https://") or url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
                raise ValueError("MCP server url must be https or local http")
        if command is None and url is None:
            raise ValueError("MCP server needs command or url")
        if command is not None and url is not None:
            raise ValueError("MCP server needs command or url, not both")
        env = _env(body.get("env", {}))
        servers[name] = McpServerConfig(name=name, command=command, url=url, env=env)
    return servers


_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "cmd.exe", "powershell"})


def _basename(part: str) -> str:
    return Path(part).name.lower()


def _refuse_shell(argv: list[str]) -> None:
    """Refuse shells by basename, including /bin/bash and env … bash."""
    if _basename(argv[0]) in _SHELLS:
        raise ValueError("MCP command must not be a shell")
    if _basename(argv[0]) != "env":
        return
    for part in argv[1:]:
        if part.startswith("-"):
            continue
        if "=" in part:
            continue
        if _basename(part) in _SHELLS:
            raise ValueError("MCP command must not be a shell")
        return


def _command(body: dict) -> tuple[str, ...] | None:
    if "command" not in body and "args" not in body:
        return None
    command = body.get("command")
    args = body.get("args", [])
    # Prefer argv list under command; allow command string + args list (Cursor-style).
    if isinstance(command, list):
        if args:
            raise ValueError("MCP command list must not also set args")
        argv = command
    elif isinstance(command, str) and command.strip():
        program = command.strip()
        if any(ch.isspace() for ch in program):
            raise ValueError("MCP command must be an argv list or a program name")
        if not isinstance(args, list):
            raise ValueError("MCP args must be a list")
        argv = [program, *args]
    else:
        raise ValueError("MCP command must be an argv list or a program name")
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise ValueError("MCP command must be a non-empty argv list")
    _refuse_shell(argv)
    return tuple(argv)


def _env(raw) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("MCP env must be a mapping")
    env = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise ValueError("MCP env values must be strings")
        if len(key) > 64 or len(value) > 2000:
            raise ValueError("MCP env entry is too long")
        env[key] = value
    return env
