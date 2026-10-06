"""MCP is an import door. A server proposes a tool call. Task grants it. A write waits for Confirm."""

import json
import re

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_KINDS = frozenset({"mcp_read", "mcp_call"})


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


class McpDoor:
    """Servers are injected. This class does not start a process and does not grow a shell."""

    def __init__(self, servers):
        self.servers = dict(servers)

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
