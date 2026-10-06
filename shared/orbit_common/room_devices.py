"""Host-owned room devices. A sentence may name one. It may not name a URL."""

ACTUATORS = {"lamp": "power", "fan": "power", "printer": "job"}
SENSORS = frozenset({"climate"})
POWERS = frozenset({"on", "off"})


def room_payload(params):
    params = params or {}
    device = str(params.get("device") or "unknown")
    if params.get("job"):
        return device + ":" + str(params["job"])
    return device + "." + str(params.get("power") or "unknown")


def room_command(goal):
    text = " ".join(str(goal).split())
    lowered = text.lower()
    for device, field in ACTUATORS.items():
        if field != "power":
            continue
        if lowered in {f"turn the {device} on", f"turn on the {device}", f"{device} on"}:
            return {"device": device, "power": "on"}
        if lowered in {f"turn the {device} off", f"turn off the {device}", f"{device} off"}:
            return {"device": device, "power": "off"}
    if lowered in {"read the climate", "what is the climate"}:
        return {"device": "climate", "read_only": True}
    if lowered.startswith("print ") and text[6:].strip():
        return {"device": "printer", "job": text[6:].strip()[:200]}
    return None
