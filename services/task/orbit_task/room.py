"""Host-side room calls. The action names the device. It does not name a URL."""

from orbit_common.room_devices import ACTUATORS, SENSORS


class HttpRoom:
    def __init__(self, base_url, token, client):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client

    async def __call__(self, action):
        kind = action.get("type")
        params = action.get("params") or {}
        device = params.get("device")
        if kind == "room_read" and device not in ACTUATORS and device not in SENSORS:
            raise ValueError("Unknown room device")
        if kind == "room_call" and device not in ACTUATORS:
            raise ValueError("Unknown room device")
        if kind not in {"room_read", "room_call"}:
            raise ValueError("Unknown room action")
        headers = {"Authorization": "Bearer " + self.token}
        path = self.base_url + "/v1/devices/" + device
        if kind == "room_read":
            response = await self.client.get(path, headers=headers, timeout=5)
        else:
            field = ACTUATORS[device]
            response = await self.client.post(
                path, headers=headers, json={field: params.get(field)}, timeout=5)
        response.raise_for_status()
        body = response.json()
        if body.get("device") != device:
            raise ValueError("Room reading is not valid")
        if kind == "room_call":
            field = ACTUATORS[device]
            if body.get(field) != params.get(field):
                raise ValueError("Room reading does not match the call")
        return body
