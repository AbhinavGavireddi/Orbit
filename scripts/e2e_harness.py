"""Orbit e2e harness: healthz, face, Confirm/Schedule/Dismiss, room grant, page under Confirm.

Uses local .env BYOK without printing secrets. Writes var/e2e-report.html.
Prefer a running stack (docker compose or scripts/run.py). Offline page checks
run in-process when Chromium is available.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shared"), str(ROOT / "services" / "task"), str(ROOT / "services" / "room")]

REPORT_PATH = ROOT / "var" / "e2e-report.html"


class Case:
    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        self.ms = 0.0
        self.live = False


def load_env():
    from dotenv import dotenv_values
    values = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def redact(text: str, secrets: list[str]) -> str:
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return out


async def check_healthz(base: str, service: str, cases: list[Case], secrets: list[str]):
    import httpx
    case = Case(f"healthz:{service}")
    case.live = True
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{base}/healthz")
            body = response.json()
            case.ok = response.status_code == 200 and body.get("status") == "ok"
            case.detail = f"HTTP {response.status_code} service={body.get('service')}"
    except Exception as error:
        case.detail = redact(f"{type(error).__name__}: {error}", secrets)
    case.ms = (time.perf_counter() - started) * 1000
    cases.append(case)


async def check_face(task_url: str, cases: list[Case], secrets: list[str]):
    import httpx
    case = Case("face")
    case.live = True
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{task_url}/face")
            text = response.text
            case.ok = (
                response.status_code == 200
                and 'id="talk"' in text
                and 'id="confirm"' in text
                and 'id="schedule"' in text
                and "Dismiss" in text
            )
            case.detail = f"HTTP {response.status_code} bytes={len(text)}"
    except Exception as error:
        case.detail = redact(f"{type(error).__name__}: {error}", secrets)
    case.ms = (time.perf_counter() - started) * 1000
    cases.append(case)


async def check_confirm_schedule_dismiss(task_url: str, device_token: str, service_token: str,
                                         cases: list[Case], secrets: list[str]):
    import httpx
    from websockets.asyncio.client import connect

    case = Case("Confirm/Schedule/Dismiss")
    case.live = True
    started = time.perf_counter()
    device = {"Authorization": "Bearer " + device_token}
    service = {"Authorization": "Bearer " + service_token}
    try:
        async with httpx.AsyncClient(base_url=task_url, timeout=20) as http:
            session = (await http.post("/v1/sessions", headers=device, json={"device_id": "e2e-face"})).json()
            sid = session["session_id"]
            ws_url = task_url.replace("http", "ws", 1) + f"/v1/devices/e2e-face/ws?session_id={sid}"
            async with connect(ws_url, additional_headers=device) as socket:
                assert (json.loads(await socket.recv()))["type"] == "connected"
                assert (json.loads(await socket.recv()))["type"] == "followup.snapshot"

                task = (await http.post("/v1/tasks", headers=device, json={
                    "session_id": sid, "device_id": "e2e-face", "kind": "goal",
                    "goal": "e2e confirm",
                })).json()
                claim = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/claim",
                    headers=service, json={"worker_id": "e2e-worker"},
                )).json()
                aid = str(uuid.uuid4())
                pending = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/actions",
                    headers=service,
                    json={
                        "worker_id": "e2e-worker", "fence": claim["fence"], "action_id": aid,
                        "action": {"type": "room_call", "params": {"device": "lamp", "power": "on"}},
                        "summary": "Allow lamp?",
                    },
                )).json()
                assert pending["status"] == "needs_confirmation"
                version = (await http.get(f"/v1/tasks/{task['task_id']}", headers=device)).json()["version"]
                await socket.send(json.dumps({
                    "type": "confirmation.response", "task_id": task["task_id"],
                    "action_id": aid, "version": version, "approved": False,
                }))
                # Drain until cancelled or timeout
                cancelled = False
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=2))
                    except TimeoutError:
                        break
                    if frame.get("type") == "task.event" and frame.get("task", {}).get("status") == "cancelled":
                        cancelled = True
                        break
                state = (await http.get(f"/v1/tasks/{task['task_id']}", headers=device)).json()
                assert state["status"] == "cancelled" or cancelled

                # Schedule + Dismiss via follow-ups
                from datetime import timedelta
                due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                rule = (await http.post(f"/v1/sessions/{sid}/followups", headers=service, json={
                    "request_id": "e2e-followup",
                    "spec": {"text": "E2E reminder", "due_at": due, "timezone": "UTC", "repeat": "none"},
                })).json()
                assert rule["status"] == "proposed"
                active = (await http.post(
                    f"/v1/sessions/{sid}/followups/{rule['id']}/confirm",
                    headers=device, json={"version": rule["version"]},
                )).json()
                assert active["status"] == "active"
                cancelled_rule = (await http.delete(
                    f"/v1/sessions/{sid}/followups/{rule['id']}", headers=device,
                )).json()
                assert cancelled_rule["status"] == "cancelled"
            await http.post(f"/v1/sessions/{sid}/close", headers=device)
        case.ok = True
        case.detail = "Confirm reject cancelled task; Schedule activated; Dismiss cancelled follow-up"
    except Exception as error:
        case.detail = redact(f"{type(error).__name__}: {error}", secrets)
    case.ms = (time.perf_counter() - started) * 1000
    cases.append(case)


async def check_room_grant(task_url: str, device_token: str, service_token: str,
                           cases: list[Case], secrets: list[str]):
    import httpx
    from websockets.asyncio.client import connect

    case = Case("room grant under Confirm")
    case.live = True
    started = time.perf_counter()
    device = {"Authorization": "Bearer " + device_token}
    service = {"Authorization": "Bearer " + service_token}
    try:
        async with httpx.AsyncClient(base_url=task_url, timeout=25) as http:
            session = (await http.post("/v1/sessions", headers=device, json={"device_id": "e2e-room"})).json()
            sid = session["session_id"]
            ws_url = task_url.replace("http", "ws", 1) + f"/v1/devices/e2e-room/ws?session_id={sid}"
            async with connect(ws_url, additional_headers=device) as socket:
                await socket.recv()
                await socket.recv()
                task = (await http.post("/v1/tasks", headers=device, json={
                    "session_id": sid, "device_id": "e2e-room", "kind": "goal", "goal": "lamp on",
                })).json()
                claim = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/claim",
                    headers=service, json={"worker_id": "e2e-room-worker"},
                )).json()
                reading = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/actions",
                    headers=service,
                    json={
                        "worker_id": "e2e-room-worker", "fence": claim["fence"],
                        "action_id": str(uuid.uuid4()),
                        "action": {"type": "room_read", "params": {"device": "lamp"}},
                        "summary": "Read lamp",
                    },
                )).json()
                assert reading["status"] == "completed"
                aid = str(uuid.uuid4())
                pending = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/actions",
                    headers=service,
                    json={
                        "worker_id": "e2e-room-worker", "fence": claim["fence"], "action_id": aid,
                        "action": {"type": "room_call", "params": {"device": "lamp", "power": "on"}},
                        "summary": "Lamp on",
                    },
                )).json()
                assert pending["status"] == "needs_confirmation"
                version = (await http.get(f"/v1/tasks/{task['task_id']}", headers=device)).json()["version"]
                await socket.send(json.dumps({
                    "type": "confirmation.response", "task_id": task["task_id"],
                    "action_id": aid, "version": version, "approved": True,
                }))
                done = None
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    result = (await http.get(
                        f"/v1/internal/tasks/{task['task_id']}/actions/{aid}",
                        headers=service,
                        params={"worker_id": "e2e-room-worker", "fence": claim["fence"]},
                    )).json()
                    if result["status"] in {"completed", "failed"}:
                        done = result
                        break
                    await asyncio.sleep(0.2)
                assert done and done["status"] == "completed"
                assert done["result"]["power"] == "on"
            await http.post(f"/v1/sessions/{sid}/close", headers=device)
        case.ok = True
        case.detail = "room_read observation; room_call after Confirm completed"
    except Exception as error:
        case.detail = redact(f"{type(error).__name__}: {error}", secrets)
    case.ms = (time.perf_counter() - started) * 1000
    cases.append(case)


async def check_page_under_confirm(task_url: str, device_token: str, service_token: str,
                                   hosts: str, cases: list[Case], secrets: list[str]):
    import httpx
    from websockets.asyncio.client import connect

    case = Case("page read + browser_act under Confirm")
    case.live = True
    started = time.perf_counter()
    if not hosts.strip():
        case.detail = "ORBIT_BROWSER_HOSTS empty; page grant refuses every host (expected refuse path only)"
        case.ok = False
        case.ms = 0
        cases.append(case)
        return
    device = {"Authorization": "Bearer " + device_token}
    service = {"Authorization": "Bearer " + service_token}
    url = "https://example.com/"
    try:
        async with httpx.AsyncClient(base_url=task_url, timeout=40) as http:
            session = (await http.post("/v1/sessions", headers=device, json={"device_id": "e2e-page"})).json()
            sid = session["session_id"]
            ws_url = task_url.replace("http", "ws", 1) + f"/v1/devices/e2e-page/ws?session_id={sid}"
            async with connect(ws_url, additional_headers=device) as socket:
                await socket.recv()
                await socket.recv()
                task = (await http.post("/v1/tasks", headers=device, json={
                    "session_id": sid, "device_id": "e2e-page", "kind": "goal", "goal": "Read example.com",
                })).json()
                claim = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/claim",
                    headers=service, json={"worker_id": "e2e-page-worker"},
                )).json()
                reading = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/actions",
                    headers=service,
                    json={
                        "worker_id": "e2e-page-worker", "fence": claim["fence"],
                        "action_id": str(uuid.uuid4()),
                        "action": {"type": "browser_read", "params": {"url": url}},
                        "summary": "Read page",
                    },
                )).json()
                assert reading["status"] == "completed", reading
                assert "example" in (reading.get("result") or {}).get("title", "").lower() or \
                    (reading.get("result") or {}).get("url") == url
                aid = str(uuid.uuid4())
                pending = (await http.post(
                    f"/v1/internal/tasks/{task['task_id']}/actions",
                    headers=service,
                    json={
                        "worker_id": "e2e-page-worker", "fence": claim["fence"], "action_id": aid,
                        "action": {"type": "browser_act", "params": {
                            "url": url, "verb": "press", "target": "Tab",
                        }},
                        "summary": "Press Tab",
                    },
                )).json()
                assert pending["status"] == "needs_confirmation", pending
                version = (await http.get(f"/v1/tasks/{task['task_id']}", headers=device)).json()["version"]
                await socket.send(json.dumps({
                    "type": "confirmation.response", "task_id": task["task_id"],
                    "action_id": aid, "version": version, "approved": True,
                }))
                done = None
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    result = (await http.get(
                        f"/v1/internal/tasks/{task['task_id']}/actions/{aid}",
                        headers=service,
                        params={"worker_id": "e2e-page-worker", "fence": claim["fence"]},
                    )).json()
                    if result["status"] in {"completed", "failed"}:
                        done = result
                        break
                    await asyncio.sleep(0.3)
                assert done and done["status"] == "completed", done
            await http.post(f"/v1/sessions/{sid}/close", headers=device)
        case.ok = True
        case.detail = "browser_read completed; browser_act waited for Confirm then completed"
    except Exception as error:
        case.detail = redact(f"{type(error).__name__}: {error}\n{traceback.format_exc()}", secrets)
    case.ms = (time.perf_counter() - started) * 1000
    cases.append(case)


async def check_page_offline(cases: list[Case]):
    """In-process allowlist + optional Chromium smoke without a live stack."""
    from orbit_common.page import AllowingPage, PlaywrightPage, check_page, host_allowed

    case = Case("offline page contract")
    started = time.perf_counter()
    try:
        assert check_page({"type": "browser_read", "params": {"url": "https://example.com/"}})["url"]
        assert host_allowed("https://example.com/x", ["example.com"])
        assert not host_allowed("https://evil.com", ["example.com"])
        assert not host_allowed("https://example.com", [])
        page = AllowingPage(PlaywrightPage(), ["example.com"])
        try:
            result = await page({"type": "browser_read", "params": {"url": "https://example.com/"}})
            assert result["url"] == "https://example.com/"
            case.ok = True
            case.detail = f"allowlist ok; chromium title={result.get('title', '')[:60]!r}"
        except RuntimeError as error:
            if "not installed" in str(error).lower() or "chromium" in str(error).lower():
                case.ok = True
                case.detail = f"allowlist ok; Chromium unavailable locally ({error})"
            else:
                raise
        finally:
            await page.inner.close()
    except Exception as error:
        case.detail = f"{type(error).__name__}: {error}"
    case.ms = (time.perf_counter() - started) * 1000
    cases.append(case)


def write_report(cases: list[Case], meta: dict) -> Path:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        status = "PASS" if case.ok else "FAIL"
        rows.append(
            "<tr class='{cls}'><td>{status}</td><td>{name}</td><td>{live}</td>"
            "<td>{ms:.0f}</td><td><pre>{detail}</pre></td></tr>".format(
                cls="pass" if case.ok else "fail",
                status=status,
                name=html.escape(case.name),
                live="live" if case.live else "offline",
                ms=case.ms,
                detail=html.escape(case.detail),
            )
        )
    passed = sum(1 for c in cases if c.ok)
    failed = len(cases) - passed
    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Orbit e2e report</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem; color: #111; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 0.5rem; vertical-align: top; }}
tr.pass td:first-child {{ color: #0a7; font-weight: 700; }}
tr.fail td:first-child {{ color: #c22; font-weight: 700; }}
pre {{ white-space: pre-wrap; margin: 0; font-size: 12px; }}
.meta {{ margin-bottom: 1rem; color: #444; }}
</style></head><body>
<h1>Orbit e2e report</h1>
<div class="meta">
<p>Generated {html.escape(meta['generated'])} (Asia/Calcutta clock on the runner).</p>
<p>Passed {passed} / {len(cases)}; failed {failed}. Live stack base: {html.escape(meta['task_url'])}.</p>
<p>Secrets were loaded from .env for live BYOK and never written into this report.</p>
</div>
<table>
<thead><tr><th>Status</th><th>Case</th><th>Mode</th><th>ms</th><th>Detail</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody></table>
</body></html>
"""
    REPORT_PATH.write_text(body)
    return REPORT_PATH


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-live", action="store_true", help="Only offline page contract")
    args = parser.parse_args()
    values = load_env()
    secrets = [
        values.get("OPENAI_API_KEY", ""),
        values.get("TYPESAFE_API_KEY", ""),
        values.get("ORBIT_DEVICE_TOKEN", ""),
        values.get("ORBIT_SERVICE_TOKEN", ""),
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("TYPESAFE_API_KEY", ""),
        os.environ.get("ORBIT_DEVICE_TOKEN", ""),
        os.environ.get("ORBIT_SERVICE_TOKEN", ""),
    ]
    task_url = os.environ.get("ORBIT_TASK_URL", values.get("ORBIT_TASK_URL", "http://127.0.0.1:8100"))
    room_url = os.environ.get("ORBIT_ROOM_URL", values.get("ORBIT_ROOM_URL", "http://127.0.0.1:8105"))
    voice_url = os.environ.get("ORBIT_VOICE_URL", values.get("ORBIT_VOICE_URL", "http://127.0.0.1:8101"))
    device = os.environ.get("ORBIT_DEVICE_TOKEN", values.get("ORBIT_DEVICE_TOKEN", ""))
    service = os.environ.get("ORBIT_SERVICE_TOKEN", values.get("ORBIT_SERVICE_TOKEN", ""))
    hosts = os.environ.get("ORBIT_BROWSER_HOSTS", values.get("ORBIT_BROWSER_HOSTS", ""))

    cases: list[Case] = []
    await check_page_offline(cases)
    if not args.skip_live:
        await check_healthz(task_url, "task", cases, secrets)
        await check_healthz(room_url, "room", cases, secrets)
        await check_healthz(voice_url, "voice", cases, secrets)
        await check_face(task_url, cases, secrets)
        if len(device) >= 32 and len(service) >= 32:
            await check_confirm_schedule_dismiss(task_url, device, service, cases, secrets)
            await check_room_grant(task_url, device, service, cases, secrets)
            await check_page_under_confirm(task_url, device, service, hosts, cases, secrets)
        else:
            missing = Case("live auth")
            missing.detail = "ORBIT_DEVICE_TOKEN / ORBIT_SERVICE_TOKEN missing or short; run scripts/setup.py"
            cases.append(missing)

    # Local wall clock is Asia/Calcutta on this machine
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    path = write_report(cases, {"generated": generated, "task_url": task_url})
    failed = [c for c in cases if not c.ok]
    print(f"Wrote {path}")
    for case in cases:
        mark = "PASS" if case.ok else "FAIL"
        print(f"{mark}\t{case.name}\t{case.detail.splitlines()[0][:160]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
