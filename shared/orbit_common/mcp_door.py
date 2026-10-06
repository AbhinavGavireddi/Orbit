"""MCP is an import door. A server proposes a tool call. Task grants it. A write waits for Confirm.

Configured servers spawn from an argv list or call a URL. No shell string. Unknown names fail closed.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
import re
from pathlib import Path

import httpx

from orbit_common.plugins import McpServerConfig, PluginsConfig, load_plugins_config

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_KINDS = frozenset({"mcp_read", "mcp_call"})
_PROTOCOL = "2024-11-05"


def check_mcp(action: dict) -> dict:
    if action.get("type") not in _KINDS or not isinstance(action.get("params"), dict):
        raise ValueError("MCP action has an unknown field")
    params = action["params"]
    if set(params) - {"server", "tool", "arguments"}:
        raise ValueError("MCP action has an unknown field")
    server, tool = params.get("server"), params.get("tool")
    arguments = params.get("arguments", {})
    if not isinstance(server, str) or not isinstance(tool, str):
        raise ValueError("MCP action needs a server and a tool")
    if _NAME.fullmatch(server) is None or _NAME.fullmatch(tool) is None:
        raise ValueError("MCP action needs a server and a tool")
    if not isinstance(arguments, dict):
        raise ValueError("MCP arguments must be an object")
    encoded = json.dumps(arguments, ensure_ascii=False)
    if len(encoded) > 2000:
        raise ValueError("MCP arguments are too long")
    return {"server": server, "tool": tool, "arguments": arguments}


def servers_from_plugins(config: PluginsConfig) -> dict:
    """Build name -> callable adapters. Empty config yields an empty door."""
    return {name: _adapter(spec) for name, spec in config.mcp_servers.items()}


def _adapter(spec: McpServerConfig):
    if spec.command is not None:
        return StdioMcpServer(spec.command, spec.env)
    return UrlMcpServer(spec.url, spec.env)


class McpDoor:
    """Servers come from plugins config or an explicit map. This class does not grow a shell."""

    def __init__(self, servers):
        self.servers = dict(servers)

    @classmethod
    def from_plugins(cls, path: Path | str | None = None) -> "McpDoor":
        return cls(servers_from_plugins(load_plugins_config(path)))

    async def __call__(self, action):
        params = check_mcp(action)
        server = self.servers.get(params["server"])
        if server is None:
            raise RuntimeError("MCP server is not configured")
        body = await server(params["tool"], params["arguments"])
        if not isinstance(body, dict):
            raise ValueError("MCP result is not an object")
        excerpt = " ".join(json.dumps(body, ensure_ascii=False).split())
        return {"server": params["server"], "tool": params["tool"], "excerpt": excerpt[:500]}


class StdioMcpServer:
    """One tool call per spawn. argv list only; never shell=True."""

    def __init__(self, argv: tuple[str, ...] | list[str], env: dict[str, str] | None = None):
        if isinstance(argv, str) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise ValueError("MCP command must be a non-empty argv list")
        self.argv = list(argv)
        self.env = dict(env or {})

    async def __call__(self, tool: str, arguments: dict) -> dict:
        env = os.environ.copy()
        env.update(self.env)
        process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            await _rpc_write(process, 1, "initialize", {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "orbit", "version": "0.1.0"},
            })
            await _rpc_read(process, 1)
            await _rpc_notify(process, "notifications/initialized", {})
            await _rpc_write(process, 2, "tools/call", {"name": tool, "arguments": arguments})
            result = await _rpc_read(process, 2)
        finally:
            if process.returncode is None:
                process.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if not isinstance(result, dict):
            raise ValueError("MCP result is not an object")
        return result


class UrlMcpServer:
    """JSON-RPC over HTTP to a configured URL. No discover-and-call without config."""

    def __init__(self, url: str, env: dict[str, str] | None = None):
        if not isinstance(url, str) or not url.strip():
            raise ValueError("MCP url is required")
        self.url = url.strip()
        self.env = dict(env or {})

    async def __call__(self, tool: str, arguments: dict) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        # Config env overrides process env (same merge idea as StdioMcpServer).
        merged = {**os.environ, **self.env}
        token = merged.get("Authorization") or merged.get("ORBIT_MCP_TOKEN")
        if token:
            headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise ValueError("MCP result is not an object")
        if body.get("error"):
            raise RuntimeError("MCP server returned an error")
        result = body.get("result", body)
        if not isinstance(result, dict):
            raise ValueError("MCP result is not an object")
        return result


async def _rpc_write(process, req_id: int, method: str, params: dict) -> None:
    await _frame_write(process, {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


async def _rpc_notify(process, method: str, params: dict) -> None:
    await _frame_write(process, {"jsonrpc": "2.0", "method": method, "params": params})


async def _frame_write(process, message: dict) -> None:
    raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
    process.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    await process.stdin.drain()


async def _rpc_read(process, req_id: int) -> dict:
    for _ in range(8):
        message = await _frame_read(process)
        if message.get("id") != req_id:
            continue
        if message.get("error"):
            raise RuntimeError("MCP server returned an error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ValueError("MCP result is not an object")
        return result
    raise RuntimeError("MCP server did not answer")


async def _frame_read(process) -> dict:
    headers = b""
    while b"\r\n\r\n" not in headers:
        chunk = await asyncio.wait_for(process.stdout.read(1), timeout=15)
        if not chunk:
            err = b""
            if process.stderr:
                err = await process.stderr.read()
            detail = err.decode("utf-8", errors="replace")[:200]
            raise RuntimeError(detail or "MCP server closed")
        headers += chunk
        if len(headers) > 2048:
            raise RuntimeError("MCP framing is invalid")
    head, _rest = headers.split(b"\r\n\r\n", 1)
    length = None
    for line in head.decode("ascii", errors="replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    if length is None or length < 2 or length > 100_000:
        raise RuntimeError("MCP framing is invalid")
    body = _rest
    while len(body) < length:
        chunk = await asyncio.wait_for(process.stdout.read(length - len(body)), timeout=15)
        if not chunk:
            raise RuntimeError("MCP server closed")
        body += chunk
    message = json.loads(body[:length].decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("MCP result is not an object")
    return message
