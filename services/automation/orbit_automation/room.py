"""Room goals. The model does not choose the device or the URL."""
import json

from orbit_common.room_devices import ACTUATORS, room_command

__all__ = ["room_command", "run_room"]


async def run_room(job, command):
    device = command["device"]
    await job.progress("Reading the " + device, phase="observing")
    before = await job.action("room_read", {"device": device}, "Read the " + device)
    if command.get("read_only"):
        return {"outcome": "completed",
                "summary": "The " + device + " reading is in the record.",
                "evidence": json.dumps(before),
                "verification": "fresh device reading"}
    field = ACTUATORS[device]
    if before.get(field) == command[field]:
        return {"outcome": "completed",
                "summary": "The " + device + " is already " + str(command[field]) + ".",
                "evidence": json.dumps(before),
                "verification": "fresh device reading"}
    await job.progress("Waiting for the on-screen confirm", phase="waiting")
    await job.action("room_call", {"device": device, field: command[field]}, "Allow this call?")
    await job.progress("Reading the " + device + " again", phase="verifying")
    after = await job.action("room_read", {"device": device}, "Read the " + device + " again")
    if after.get(field) != command[field]:
        return {"outcome": "blocked",
                "summary": "The " + device + " reading does not match the call.",
                "evidence": json.dumps(after),
                "verification": "fresh device reading"}
    return {"outcome": "completed",
            "summary": "The " + device + " is " + str(after[field]) + ".",
            "evidence": json.dumps(after),
            "verification": "fresh device reading"}
