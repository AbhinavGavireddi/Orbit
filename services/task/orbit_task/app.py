import asyncio
import json
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import anyio
import httpx
from fastapi import Query, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from redis.asyncio import Redis

from orbit_common.config import Settings
from orbit_common.contracts import ActionRequest, Artifact, Claim, Event, MemoryCreate, Owner, SessionCreate, TaskCreate
from orbit_common.effects import Effects
from orbit_common.mcp_door import McpDoor
from orbit_common.page import AllowingPage, PlaywrightPage
from orbit_common.security import authorized
from .actions import ActionResults, RiskAssessor
from .room import HttpRoom
from .store import Conflict, Missing, Store
from .followups import FollowUps, FollowUpProposal, FollowUpConfirmation, FollowUpReceipt

FACE_PAGE = Path(__file__).with_name("face.html")


def create_app(settings=None, redis=None):
    config = settings or Settings()
    db = redis or Redis.from_url(config.redis_url, decode_responses=True)
    room_http = httpx.AsyncClient()
    room = HttpRoom(config.orbit_room_url, config.orbit_service_token, room_http)
    page = AllowingPage(PlaywrightPage(), config.orbit_browser_hosts.split(","))
    door = McpDoor.from_plugins(config.orbit_plugins_config)
    store = Store(db, config.orbit_artifact_dir, effects=Effects({
        "room_read": room, "room_call": room,
        "browser_read": page, "browser_act": page,
        "mcp_read": door, "mcp_call": door,
    }))
    followups = FollowUps(db)
    results = ActionResults(store)
    risk_http = httpx.AsyncClient()
    assessor = RiskAssessor(config, risk_http)

    @asynccontextmanager
    async def lifespan(app):
        config.require_auth()
        await db.ping()
        scheduler = asyncio.create_task(deliver_followups())
        try:
            yield
        finally:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        await risk_http.aclose()
        await room_http.aclose()
        await page.inner.close()
        await db.aclose()

    app = FastAPI(title="Orbit Task Authority", version="1.0.0", lifespan=lifespan)
    app.state.store = store

    async def device(authorization: str | None = Header(default=None)):
        if not authorized(authorization, config.orbit_device_token):
            raise HTTPException(401, "Device authentication required")

    async def service(authorization: str | None = Header(default=None)):
        if not authorized(authorization, config.orbit_service_token):
            raise HTTPException(401, "Service authentication required")

    async def either(authorization: str | None = Header(default=None)):
        if not (authorized(authorization, config.orbit_device_token)
                or authorized(authorization, config.orbit_service_token)):
            raise HTTPException(401, "Authentication required")

    @app.exception_handler(Conflict)
    async def conflict_handler(_, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(Missing)
    async def missing_handler(_, error):
        return JSONResponse({"detail": "Resource expired or missing"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid_handler(_, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "task"}

    @app.get("/readyz")
    async def ready():
        try:
            await db.ping()
        except Exception:
            raise HTTPException(503, "Redis unavailable")
        return {"ready": True}

    @app.get("/face")
    async def face():
        return FileResponse(FACE_PAGE)

    @app.post("/v1/sessions/{sid}/face-ticket", dependencies=[Depends(device)])
    async def face_ticket(sid: uuid.UUID):
        session = await store.active_session(str(sid))
        value = session["device_id"] + "\n" + session["session_id"]
        ticket = secrets.token_urlsafe(32)
        voice_ticket = secrets.token_urlsafe(32)
        await db.set("orbit:face-ticket:" + ticket, value, ex=60)
        await db.set("orbit:face-ticket:" + voice_ticket, value, ex=60)
        return {"ticket": ticket, "voice_ticket": voice_ticket}

    @app.post("/v1/sessions", dependencies=[Depends(device)])
    async def session(body: SessionCreate):
        return await store.session(body.device_id)

    @app.get("/v1/sessions/{sid}", dependencies=[Depends(either)])
    async def get_session(sid: uuid.UUID):
        return await store.active_session(str(sid))

    @app.post("/v1/sessions/{sid}/close", dependencies=[Depends(either)])
    async def close(sid: uuid.UUID):
        await store.close(str(sid))
        return {"closed": True}

    @app.post("/v1/sessions/{sid}/stop", dependencies=[Depends(either)])
    async def stop(sid: uuid.UUID):
        return await store.stop(str(sid))

    @app.get("/v1/sessions/{sid}/tasks", dependencies=[Depends(either)])
    async def session_tasks(sid: uuid.UUID):
        return {"tasks": await store.session_tasks(str(sid))}

    async def followup_device(sid):
        return (await store.active_session(str(sid)))["device_id"]

    @app.get("/v1/sessions/{sid}/followups", dependencies=[Depends(either)])
    async def list_followups(sid: uuid.UUID):
        device_id = await followup_device(sid)
        return {"rules": await followups.list(device_id), "due": await followups.due(device_id)}

    @app.post("/v1/sessions/{sid}/followups", dependencies=[Depends(either)])
    async def propose_followup(sid: uuid.UUID, body: FollowUpProposal):
        device_id = await followup_device(sid)
        async with store.transaction():
            session = await store.active_session(str(sid))
            if body.generation != session["generation"]:
                raise Conflict("Follow-up proposal belongs to an earlier stop generation")
            rule = await followups.propose(device_id, body.request_id, body.spec.model_dump(mode="json"), body.rule_id)
            await store.emit(device_id, {"type": "followup.changed", "session_id": str(sid), "rule": rule})
        return rule

    @app.post("/v1/sessions/{sid}/followups/{rid}/confirm", dependencies=[Depends(device)])
    async def confirm_followup(sid: uuid.UUID, rid: str, body: FollowUpConfirmation):
        device_id = await followup_device(sid)
        async with store.transaction():
            rule = await followups.confirm(device_id, rid, body.version)
            await store.emit(device_id, {"type": "followup.changed", "session_id": str(sid), "rule": rule})
        return rule

    @app.delete("/v1/sessions/{sid}/followups/{rid}", dependencies=[Depends(either)])
    async def cancel_followup(sid: uuid.UUID, rid: str):
        device_id = await followup_device(sid)
        async with store.transaction():
            rule = await followups.cancel(device_id, rid)
            await store.emit(device_id, {"type": "followup.changed", "session_id": str(sid), "rule": rule})
        return rule

    @app.post("/v1/sessions/{sid}/followup-receipts", dependencies=[Depends(either)])
    async def acknowledge_followup(sid: uuid.UUID, body: FollowUpReceipt):
        device_id = await followup_device(sid)
        async with store.transaction():
            result = await followups.acknowledge(device_id, body.delivery_id)
            for rule in result["rules"]:
                await store.emit(device_id, {"type": "followup.changed", "session_id": str(sid), "rule": rule})
        return result

    async def deliver_followups():
        while True:
            try:
                for device_id in await db.smembers("orbit:followup-devices"):
                    sid = await db.get("orbit:current:" + device_id)
                    if not sid:
                        continue
                    try:
                        await store.active_session(sid)
                    except (Conflict, Missing):
                        continue
                    async with store.transaction():
                        for delivery in await followups.due(device_id):
                            await followups.publish(device_id, sid, delivery)
            except Exception:
                # No payloads or private context in diagnostics; retry without authorizing any task.
                pass
            await asyncio.sleep(15)

    @app.post("/v1/tasks", status_code=202, dependencies=[Depends(either)])
    async def create(body: TaskCreate, authorization: str | None = Header(default=None)):
        if body.route_source and not authorized(authorization, config.orbit_service_token):
            raise HTTPException(403, "Routing proposals require service authentication")
        return await store.create(str(body.session_id), body.device_id, body.kind, body.goal,
                                  body.request_id, body.generation, body.turn_id, body.route_source,
                                  body.capability, body.skill_name, body.constraints, body.completion_criteria)

    @app.get("/v1/memories", dependencies=[Depends(service)])
    async def list_memories(backfill: bool = Query(False)):
        records = await store.memories()
        if backfill:
            await store.backfill_embeddings(records)
        return {"memories": records}

    @app.post("/v1/memories", dependencies=[Depends(service)])
    async def create_memory(body: MemoryCreate):
        return await store.remember(body.text, body.kind, body.source_turn)

    @app.delete("/v1/memories/{mid}", dependencies=[Depends(service)])
    async def delete_memory(mid: str):
        return await store.forget(mid)

    @app.get("/v1/tasks/{tid}", dependencies=[Depends(either)])
    async def get_task(tid: uuid.UUID):
        return await store.task(str(tid))

    @app.post("/v1/tasks/{tid}/cancel", dependencies=[Depends(either)])
    async def cancel(tid: uuid.UUID):
        return await store.cancel(str(tid))

    @app.post("/v1/internal/tasks/{tid}/claim", dependencies=[Depends(service)])
    async def claim(tid: uuid.UUID, body: Claim):
        return await store.claim(str(tid), body.worker_id)

    @app.post("/v1/internal/tasks/{tid}/heartbeat", dependencies=[Depends(service)])
    async def heartbeat(tid: uuid.UUID, body: Owner):
        return await store.heartbeat(str(tid), body.worker_id, body.fence)

    @app.post("/v1/internal/tasks/{tid}/event", dependencies=[Depends(service)])
    async def event(tid: uuid.UUID, body: Event):
        return await store.event(str(tid), body.worker_id, body.fence, status=body.status,
                                 progress=body.progress, result=body.result, error=body.error, phase=body.phase)

    @app.post("/v1/internal/tasks/{tid}/actions", dependencies=[Depends(service)])
    async def action(tid: uuid.UUID, body: ActionRequest, wait: bool = False):
        async def submit():
            task = await store.owned(str(tid), body.worker_id, body.fence)
            risk = await assessor.assess(str(tid), str(body.action_id), body.action.model_dump(), task["goal"])
            return await store.action(str(tid), body.worker_id, body.fence, str(body.action_id),
                                      body.action.model_dump(), body.summary, risk=risk)
        if wait or body.action.type in {"screenshot", "ax_snapshot"}:
            return await results.wait(str(tid), body.worker_id, body.fence, str(body.action_id), submit)
        return await submit()

    @app.get("/v1/internal/tasks/{tid}/actions/{aid}", dependencies=[Depends(service)])
    async def action_result(tid: uuid.UUID, aid: uuid.UUID, worker_id: str, fence: int, wait: bool = False):
        if wait:
            return await results.wait(str(tid), worker_id, fence, str(aid))
        return await store.action_result(str(tid), worker_id, fence, str(aid))

    @app.post("/v1/internal/artifacts", dependencies=[Depends(service)])
    async def artifact(body: Artifact):
        return await store.artifact(str(body.task_id), str(body.artifact_id), body.filename, body.media_type)

    @app.get("/v1/artifacts/{aid}", dependencies=[Depends(device)])
    async def download(aid: uuid.UUID):
        metadata = await store.get_artifact(str(aid))
        path = config.orbit_artifact_dir / str(aid)
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Artifact unavailable")
        return FileResponse(path, media_type=metadata["media_type"], filename=metadata["filename"])

    def frame_session(frame):
        return frame.get("session_id") or frame.get("task", {}).get("session_id")

    async def stream_events(ws, sid, device_id, cursor, guard=None, task_only=False):
        while True:
            if guard and await db.get("orbit:connection:" + device_id) != guard:
                await ws.close(code=4009, reason="Connection replaced")
                return
            rows = await db.xread({"orbit:device:" + device_id: cursor}, block=1000, count=30)
            for _, messages in rows:
                for mid, payload in messages:
                    cursor = mid
                    frame = json.loads(payload["json"])
                    if frame_session(frame) != sid:
                        continue
                    if task_only and frame["type"] not in {"task.event", "session.closed", "followup.due", "followup.changed"}:
                        continue
                    if frame["type"] == "device.command":
                        try:
                            task = await store.task(frame["task_id"])
                            await store.owned(task["task_id"], task.get("worker_id"), frame["fence"])
                        except (Conflict, Missing):
                            continue
                    await ws.send_json(frame)

    async def last_cursor(device_id):
        last = await db.xrevrange("orbit:device:" + device_id, count=1)
        return last[0][0] if last else "0-0"

    @app.websocket("/v1/sessions/{sid}/events")
    async def events(ws: WebSocket, sid: str):
        if not authorized(ws.headers.get("authorization"), config.orbit_service_token):
            await ws.close(code=4401)
            return
        try:
            session = await store.active_session(sid)
        except (Conflict, Missing):
            await ws.close(code=4409)
            return
        await ws.accept()
        cursor = await last_cursor(session["device_id"])
        workers = []
        try:
            await ws.send_json({"type": "task.snapshot", "tasks": await store.session_tasks(sid)})
            rules = await followups.list(session["device_id"])
            await ws.send_json({"type": "followup.snapshot", "rules": rules, "due": await followups.due(session["device_id"])})

            async def receive_until_disconnect():
                while True:
                    if (await ws.receive())["type"] == "websocket.disconnect":
                        return

            workers = [asyncio.create_task(stream_events(ws, sid, session["device_id"], cursor, task_only=True)),
                       asyncio.create_task(receive_until_disconnect())]
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for worker in done:
                worker.result()
        except (WebSocketDisconnect, RuntimeError, Conflict, Missing):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    @app.websocket("/v1/devices/{device_id}/ws")
    async def connection(ws: WebSocket, device_id: str, session_id: str, ticket: str | None = None):
        header_ok = authorized(ws.headers.get("authorization"), config.orbit_device_token)
        saved = await db.get("orbit:face-ticket:" + (ticket or ""))
        expected = device_id + "\n" + session_id
        ticket_ok = (isinstance(saved, str) and len(saved) == len(expected)
                     and secrets.compare_digest(saved, expected))
        if ticket_ok:
            await db.delete("orbit:face-ticket:" + ticket)
        if not header_ok and not ticket_ok:
            await ws.close(code=4401)
            return
        try:
            await store.active_session(session_id, device_id)
        except (Conflict, Missing):
            await ws.close(code=4409)
            return
        nonce = str(uuid.uuid4())
        await ws.accept()
        cursor = await last_cursor(device_id)
        await db.set("orbit:connection:" + device_id, nonce, ex=35)
        await ws.send_json({"type": "connected", "session_id": session_id})
        rules = await followups.list(device_id)
        await ws.send_json({"type": "followup.snapshot", "rules": rules, "due": await followups.due(device_id)})
        sender = asyncio.create_task(stream_events(ws, session_id, device_id, cursor, nonce))

        async def receive():
            while True:
                frame = await ws.receive_json()
                if await db.get("orbit:connection:" + device_id) != nonce:
                    return
                await db.expire("orbit:connection:" + device_id, 35)
                try:
                    kind = frame.get("type")
                    if kind == "ping":
                        await ws.send_json({"type": "pong"})
                    elif kind == "device.result":
                        await store.device_result(session_id, frame["task_id"], frame["action_id"],
                            frame["fence"], frame["ok"], frame.get("result", {}), frame.get("error"))
                    elif kind == "confirmation.response":
                        if not isinstance(frame.get("approved"), bool):
                            raise ValueError("Approval must be boolean")
                        await store.confirm(session_id, frame["task_id"], frame["action_id"],
                                            frame["version"], frame["approved"])
                    elif kind == "device.takeover":
                        task = await store.task(frame["task_id"])
                        if task["session_id"] != session_id:
                            raise Conflict("Task/session mismatch")
                        await store.cancel(task["task_id"], "Paused by physical user input; start a new task")
                except (Conflict, Missing, KeyError, ValueError) as error:
                    await ws.send_json({"type": "error", "message": str(error)})

        receiver = asyncio.create_task(receive())
        try:
            done, _ = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_COMPLETED)
            for future in done:
                with suppress(WebSocketDisconnect, RuntimeError):
                    future.result()
        finally:
            with anyio.CancelScope(shield=True):
                sender.cancel()
                receiver.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
                if await db.get("orbit:connection:" + device_id) == nonce:
                    await db.delete("orbit:connection:" + device_id)
                    with suppress(Missing):
                        await store.close(session_id)

    return app
