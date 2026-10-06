"""Real HTTP/WebSocket broker test with a simulated Mac; no model or desktop calls.

Run with automation/research workers stopped so this test can explicitly claim jobs.
Use --workers with two automation replicas running to exercise the real consumers.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from orbit_common.config import Settings  # noqa: E402


async def worker_handoff(request, socket, sid, identity):
    """Two real consumers, one simulated Mac, no provider or actual Notes calls."""
    commands = {}

    async def ready(tid):
        async with asyncio.timeout(25):
            while True:
                event = json.loads(await socket.recv())
                if event["type"] == "device.command":
                    task_id = event["task_id"]
                    seen = commands.setdefault(task_id, [])
                    assert event["action_id"] not in [x[0] for x in seen], "Action delivered twice"
                    kind = event["action"]["type"]
                    seen.append((event["action_id"], kind, event["fence"]))
                    assert kind in {"open_app", "notes_create", "dictation_start"}
                    result = {"note_id": "simulated-" + task_id} if kind == "notes_create" else {}
                    await socket.send(json.dumps({"type": "device.result", "task_id": task_id,
                        "action_id": event["action_id"], "fence": event["fence"], "ok": True, "result": result}))
                if event["type"] == "task.event" and event["task"]["task_id"] == tid:
                    task = event["task"]
                    assert task["status"] not in {"failed", "cancelled"}, task.get("error")
                    if (task.get("result") or {}).get("mode") == "dictation_ready":
                        assert [row[1] for row in commands[tid]] == ["open_app", "notes_create", "dictation_start"]
                        assert len({row[2] for row in commands[tid]}) == 1
                        return task

    body = {"session_id": sid, "device_id": identity, "kind": "dictation",
            "goal": "Simulated worker test; no real desktop", "request_id": "worker-first"}
    first = await request("POST", "/v1/tasks", json=body)
    assert (await request("POST", "/v1/tasks", json=body))["task_id"] == first["task_id"]
    first = await ready(first["task_id"])
    second = await request("POST", "/v1/tasks", json={**body, "request_id": "worker-second"})
    await asyncio.sleep(0.4)
    assert (await request("GET", "/v1/tasks/" + second["task_id"]))["status"] == "queued"
    await request("POST", "/v1/tasks/" + first["task_id"] + "/cancel")
    second = await ready(second["task_id"])
    assert second["worker_id"] != first["worker_id"], "Run with --scale automation=2"
    assert second["fence"] > first["fence"]
    await request("POST", "/v1/tasks/" + second["task_id"] + "/cancel")
    print("PASS: two real worker replicas, single device lease, retry deduplication, exact three-action setup and fenced cancellation handoff")


async def main(workers=False):
    config = Settings(_env_file=ROOT / ".env")
    device = {"Authorization": "Bearer " + config.orbit_device_token}
    service = {"Authorization": "Bearer " + config.orbit_service_token}
    base = config.orbit_task_url
    async with httpx.AsyncClient(base_url=base, headers=device, timeout=30) as http:
        async def request(method, path, **kwargs):
            response = await http.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        identity = "smoke-" + uuid.uuid4().hex[:12]
        session = await request("POST", "/v1/sessions", json={"device_id": identity})
        sid = session["session_id"]
        try:
            async with connect(base.replace("http", "ws", 1) + f"/v1/devices/{identity}/ws?session_id={sid}",
                               additional_headers=device) as socket:
                assert json.loads(await socket.recv())["type"] == "connected"
                if workers:
                    await worker_handoff(request, socket, sid, identity)
                    return

                async def frame(kind):
                    async with asyncio.timeout(10):
                        while True:
                            event = json.loads(await socket.recv())
                            if event["type"] == kind:
                                return event

                body = {"session_id": sid, "device_id": identity, "kind": "automation",
                        "goal": "Simulated broker check; no real desktop", "request_id": "retry-once"}
                task = await request("POST", "/v1/tasks", json=body)
                retry = await request("POST", "/v1/tasks", json=body)
                assert task["task_id"] == retry["task_id"]
                tid = task["task_id"]
                prefix = "/v1/internal/tasks/" + tid
                owner = await request("POST", prefix + "/claim", headers=service, json={"worker_id": "smoke-a"})
                lease = {"worker_id": "smoke-a", "fence": owner["fence"]}
                competitor = await http.post(prefix + "/claim", headers=service, json={"worker_id": "smoke-b"})
                assert competitor.status_code == 409

                aid = str(uuid.uuid4())
                begin = time.perf_counter()
                pending = asyncio.create_task(request("POST", prefix + "/actions", headers=service, json={
                    **lease, "action_id": aid, "action": {"type": "screenshot", "params": {}}, "summary": "Simulated screen"}))
                command = await frame("device.command")
                assert command["action_id"] == aid
                await socket.send(json.dumps({"type": "device.result", "task_id": tid, "action_id": aid,
                    "fence": lease["fence"], "ok": True, "result": {"image_base64": "SIMULATED_PIXELS", "width": 1, "height": 1}}))
                result = await pending
                assert result["result"]["image_base64"] == "SIMULATED_PIXELS"
                roundtrip = (time.perf_counter() - begin) * 1000
                stored = await request("GET", prefix + "/actions/" + aid, headers=service, params=lease)
                assert "image_base64" not in stored["result"]

                ax_aid = str(uuid.uuid4())
                ax_pending = asyncio.create_task(request("POST", prefix + "/actions", headers=service, json={
                    **lease, "action_id": ax_aid, "action": {"type": "ax_snapshot", "params": {}},
                    "summary": "Read controls"}))
                ax_command = await frame("device.command")
                assert ax_command["action_id"] == ax_aid
                controls = [{"id": "7", "role": "AXButton", "title": "Calendar", "description": "month"}]
                await socket.send(json.dumps({"type": "device.result", "task_id": tid, "action_id": ax_aid,
                    "fence": lease["fence"], "ok": True, "result": {"controls": controls, "app": "com.apple.Calendar"}}))
                ax_result = await ax_pending
                assert ax_result["result"]["controls"] == controls
                ax_stored = await request("GET", prefix + "/actions/" + ax_aid, headers=service, params=lease)
                assert ax_stored["result"] == {}

                aid = str(uuid.uuid4())
                await request("POST", prefix + "/actions", headers=service, json={**lease,
                    "action_id": aid, "action": {"type": "computer", "params": {"actions": [{"type": "click", "x": 1, "y": 1}]}},
                    "summary": "Simulated click"})
                approval = await frame("confirmation")
                assert approval["summary"].startswith("Review summary:")
                assert "Exact action JSON" in approval["summary"]
                assert "Simulated click" not in approval["summary"]
                await socket.send(json.dumps({"type": "confirmation.response", "task_id": tid, "action_id": aid,
                    "version": approval["version"] - 1, "approved": True}))
                assert "Approval" in (await frame("error"))["message"]
                await request("POST", f"/v1/sessions/{sid}/stop")
                assert (await request("GET", "/v1/tasks/" + tid))["status"] == "cancelled"
                stale = await http.post("/v1/tasks", json={**body, "request_id": "late-submission"})
                assert stale.status_code == 409
                print(f"PASS: real broker auth, idempotency, exclusive claim, transient screenshot ({roundtrip:.1f} ms simulated round-trip), stale approval and stop generation")
        finally:
            await http.post(f"/v1/sessions/{sid}/close")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", action="store_true", help="Use two live automation replicas instead of explicit claims")
    asyncio.run(main(parser.parse_args().workers))
