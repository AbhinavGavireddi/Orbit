"""Host effects share one door. The store records the grant. An adapter performs the call."""

HOST_READS = frozenset({"room_read", "browser_read", "mcp_read"})
HOST_WRITES = frozenset({"room_call", "browser_act", "mcp_call"})


def is_host(kind) -> bool:
    return kind in HOST_READS or kind in HOST_WRITES


def is_read(kind) -> bool:
    return kind in HOST_READS


class Effects:
    """One callable per action type. A new world registers here. The store does not grow a branch."""

    def __init__(self, adapters):
        self.adapters = dict(adapters)

    async def __call__(self, action):
        kind = action.get("type")
        adapter = self.adapters.get(kind)
        if adapter is None:
            raise RuntimeError("No host adapter for this action")
        return await adapter(action)
