"""One live public-topic research smoke check, with no native application action."""
import asyncio
from datetime import datetime, timezone
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


async def main():
    config = Settings(_env_file=ROOT / ".env")
    headers = {"Authorization": "Bearer " + config.orbit_device_token}
    output = ROOT / "var/validation/live-research"
    output.mkdir(parents=True, exist_ok=True)
    report = {"measured_at": datetime.now(timezone.utc).isoformat(), "measurement": "backend_only"}
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=config.orbit_task_url, headers=headers, timeout=30) as http:
        response = await http.post("/v1/sessions", json={"device_id": "research-probe-" + uuid.uuid4().hex[:12]})
        response.raise_for_status()
        session = response.json()
        sid = session["session_id"]
        try:
            url = config.orbit_task_url.replace("http", "ws", 1) + f"/v1/devices/{session['device_id']}/ws?session_id={sid}"
            async with connect(url, additional_headers=headers) as socket, asyncio.timeout(210):
                response = await http.post("/v1/tasks", json={"session_id": sid, "device_id": session["device_id"],
                    "kind": "research", "request_id": "one-public-report", "goal":
                    "Using primary sources only, compare macOS SpeechAnalyzer on-device speech with streaming cloud speech recognition "
                    "for an English desktop assistant. Cover capabilities, permissions, privacy, latency measurement limits and a "
                    "brief practical recommendation. Do not infer achieved latency from vendor claims."})
                response.raise_for_status()
                task_id = response.json()["task_id"]
                async for raw in socket:
                    frame = json.loads(raw)
                    if frame["type"] == "device.command":
                        raise RuntimeError("Research unexpectedly requested a native action")
                    if frame["type"] != "task.event" or frame["task"]["task_id"] != task_id:
                        continue
                    task = frame["task"]
                    if task["status"] not in {"completed", "failed", "cancelled"}:
                        continue
                    report["status"] = task["status"]
                    if task["status"] == "completed":
                        result = task["result"]
                        report["source_count"] = result["source_count"]
                        report["artifacts"] = []
                        for artifact in result["artifacts"]:
                            content = await http.get("/v1/artifacts/" + artifact["artifact_id"])
                            content.raise_for_status()
                            filename = Path(artifact["filename"]).name
                            (output / filename).write_bytes(content.content)
                            report["artifacts"].append(filename)
                    break
        except Exception as error:
            report.update(status="incomplete", error_class=type(error).__name__)
        finally:
            await http.post(f"/v1/sessions/{sid}/close")
    report["elapsed_ms"] = round((time.monotonic() - started)*1000, 2)
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
