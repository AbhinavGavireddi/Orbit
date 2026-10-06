import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from orbit_common.effects import Effects, is_host, is_read
from orbit_common.embeddings import embed_texts
from orbit_common.routing import CAPABILITIES
from .policy import approval_summary, card_payload, validate_action
from .turns import TurnCoordinator, scoped_key

from orbit_common.contracts import TERMINAL
TTL = 86400


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


PERSISTED_RESULT_KEYS = {
    "width", "height", "app_bundle_id", "note_id", "ok", "inserted", "clicked",
    "device", "power", "job", "temperature", "humidity",
    "url", "title", "excerpt", "server", "tool",
}


class Store:
    def __init__(self, redis, artifacts: Path, room=None, effects=None):
        self.redis, self.artifacts = redis, artifacts
        if effects is None and room is not None:
            effects = Effects({"room_read": room, "room_call": room})
        self.effects = effects
        self.turns = TurnCoordinator(redis, TTL)
        artifacts.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def transaction(self):
        # Short atomic state changes only; never hold across model/network calls.
        async with self.redis.lock("orbit:state-lock", timeout=10, blocking_timeout=3):
            yield

    async def read(self, key):
        value = await self.redis.get(key)
        if value is None:
            raise Missing(key.split(":")[1])
        return json.loads(value)

    async def write(self, key, value):
        await self.redis.set(key, json.dumps(value), ex=TTL)

    async def emit(self, device, frame):
        await self.redis.xadd(f"orbit:device:{device}", {"json": json.dumps(frame)}, maxlen=1000)

    async def save_task(self, task):
        task["version"] += 1
        task["updated_at"] = time.time()
        terminal = task["status"] in TERMINAL
        lease_key = "orbit:lease:" + task["device_id"]
        lease = await self.redis.get(lease_key) if terminal else None
        # A lost EXEC reply may hide success from the caller, but cannot leave
        # terminal state without its event, wakeup or native cancellation.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set("orbit:task:" + task["task_id"], json.dumps(task), ex=TTL)
            pipe.xadd("orbit:device:" + task["device_id"],
                      {"json": json.dumps({"type": "task.event", "task": task})}, maxlen=1000)
            if terminal:
                pipe.delete("orbit:owner:" + task["task_id"])
                if lease and json.loads(lease)["task_id"] == task["task_id"]:
                    pipe.delete(lease_key)
                pipe.publish("orbit:task-signal:" + task["task_id"], "changed")
                pipe.xadd("orbit:device:" + task["device_id"], {"json": json.dumps({
                    "type": "device.cancel", "task_id": task["task_id"],
                    "session_id": task["session_id"]})}, maxlen=1000)
            await pipe.execute()

    async def task(self, tid):
        return await self.read("orbit:task:" + tid)

    async def session_tasks(self, sid):
        await self.active_session(sid)
        tasks = []
        for tid in await self.redis.smembers("orbit:session-tasks:" + sid):
            try:
                tasks.append(await self.task(tid))
            except Missing:
                continue
        return tasks

    async def active_session(self, sid, device=None):
        session = await self.read("orbit:session:" + sid)
        if not session["active"] or (device and session["device_id"] != device):
            raise Conflict("Session is closed or belongs to another device")
        return session

    async def session(self, device):
        async with self.transaction():
            old = await self.redis.get("orbit:current:" + device)
            if old:
                await self._close(old)
            sid = str(uuid.uuid4())
            session = {"session_id": sid, "device_id": device, "active": True, "generation": 0}
            await self.write("orbit:session:" + sid, session)
            await self.redis.set("orbit:current:" + device, sid, ex=TTL)
            return session

    async def _close(self, sid):
        session = await self.read("orbit:session:" + sid)
        session["active"] = False
        await self.write("orbit:session:" + sid, session)
        for tid in await self.redis.smembers("orbit:session-tasks:" + sid):
            try:
                task = await self.task(tid)
            except Missing:
                continue
            if task["status"] not in TERMINAL:
                await self._end(task, "cancelled", "Session closed")
        await self.emit(session["device_id"], {"type": "session.closed", "session_id": sid})

    async def close(self, sid):
        async with self.transaction():
            await self._close(sid)

    async def stop(self, sid):
        async with self.transaction():
            session = await self.active_session(sid)
            session["generation"] += 1
            await self.write("orbit:session:" + sid, session)
            for tid in await self.redis.smembers("orbit:session-tasks:" + sid):
                with_missing = await self.redis.get("orbit:task:" + tid)
                if with_missing:
                    task = json.loads(with_missing)
                    if task["status"] not in TERMINAL:
                        await self._end(task, "cancelled", "Stopped by user")
            return session

    async def create(self, sid, device, kind, goal, request_id=None, generation=0,
                     turn_id=None, route_source=None, capability=None, skill_name=None,
                     constraints=None, completion_criteria=None):
        constraints, completion_criteria = constraints or [], completion_criteria or []
        if kind not in {"goal", "automation", "research", "dictation"} or (kind == "goal" and not goal.strip()):
            raise ValueError("Invalid task goal or kind")
        if bool(turn_id) != bool(route_source) or (turn_id and not request_id):
            raise ValueError("Routed tasks require a turn, source and request ID")
        if route_source not in {None, "jev", "realtime"}:
            raise ValueError("Unknown routing source")
        if capability == "computer":
            if kind != "automation" or not isinstance(goal, str):
                raise ValueError("Computer task must carry the user's request")
            goal = " ".join(goal.split())
            if not goal or len(goal) > 500:
                raise ValueError("Computer task must carry the user's request")
        elif capability == "research":
            if kind != "research" or not isinstance(goal, str):
                raise ValueError("Research task must carry the user's request")
            goal = " ".join(goal.split())
            if not goal or len(goal) > 4000:
                raise ValueError("Research task must carry the user's request")
        elif capability == "skill":
            if kind != "automation" or not isinstance(goal, str) or not isinstance(skill_name, str) or not skill_name:
                raise ValueError("Skill task must name one installed skill and carry the user's request")
            goal = " ".join(goal.split())
            if not goal or len(goal) > 4000 or len(skill_name) > 80:
                raise ValueError("Skill task must name one installed skill and carry the user's request")
        elif capability is not None:
            supported = CAPABILITIES.get(capability)
            if supported is None or kind != supported.kind or goal != supported.goal:
                raise ValueError("Fast capability must have its exact canonical task")
        if route_source == "jev" and capability is None:
            raise ValueError("Jev may only propose a supported fast capability")
        async with self.transaction():
            session = await self.active_session(sid, device)
            if generation != session["generation"]:
                raise Conflict("Submission belongs to an earlier stop generation")
            request_key = scoped_key("request", sid, generation, request_id) if request_id else None
            if request_key and (prior := await self.redis.get(request_key)):
                previous = await self.task(prior)
                if (previous["kind"], previous["goal"], previous["device_id"], previous.get("capability"),
                    previous.get("turn_id"), previous.get("route_source"), previous.get("skill_name"),
                    previous.get("constraints", []), previous.get("completion_criteria", [])) != (
                        kind, goal, device, capability, turn_id, route_source, skill_name, constraints, completion_criteria):
                    raise Conflict("Request ID already used with a different payload")
                return previous
            turn_key = scoped_key("turn", sid, generation, turn_id) if turn_id else None
            owner = await self.turns.owner(turn_key)
            if existing := self.turns.existing_task(owner, route_source):
                return await self.task(existing)
            task = dict(task_id=str(uuid.uuid4()), session_id=sid, device_id=device,
                        kind=kind, goal=goal, status="queued", version=0,
                        generation=generation, turn_id=turn_id, route_source=route_source, capability=capability,
                        skill_name=skill_name, constraints=constraints, completion_criteria=completion_criteria,
                        phase="planning",
                        result=None, error=None, created_at=time.time())
            task["version"], task["updated_at"] = 1, time.time()
            queue = "research" if kind == "research" else "automation"
            # Commit task, idempotency mapping and queue entry in one Redis transaction.
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set("orbit:task:" + task["task_id"], json.dumps(task), ex=TTL)
                if request_key:
                    pipe.set(request_key, task["task_id"], ex=TTL)
                if turn_key and not owner:
                    self.turns.commit(pipe, turn_key, route_source, task["task_id"])
                pipe.sadd("orbit:session-tasks:" + sid, task["task_id"])
                pipe.expire("orbit:session-tasks:" + sid, TTL)
                pipe.xadd("orbit:jobs:" + queue, {"task_id": task["task_id"]})
                pipe.xadd("orbit:device:" + device, {"json": json.dumps({"type": "task.event", "task": task})}, maxlen=1000)
                await pipe.execute()
            return task

    async def _end(self, task, status, error=None):
        task["status"], task["error"] = status, error
        await self.save_task(task)

    async def cancel(self, tid, reason="Cancelled by user"):
        async with self.transaction():
            task = await self.task(tid)
            if task["status"] not in TERMINAL:
                await self._end(task, "cancelled", reason)
            return task

    async def claim(self, tid, worker):
        async with self.transaction():
            task = await self.task(tid)
            await self.active_session(task["session_id"])
            if task["status"] != "queued":
                if task["status"] not in TERMINAL and not await self.redis.exists("orbit:owner:" + tid):
                    await self._end(task, "failed", "Worker disconnected; state uncertain. Start a new task.")
                raise Conflict("Task is not queued")
            if task["kind"] != "research" and await self.redis.exists("orbit:lease:" + task["device_id"]):
                raise Conflict("Device busy")
            fence = await self.redis.incr("orbit:fence:" + task["device_id"])
            owner = {"worker_id": worker, "fence": fence, "task_id": tid}
            await self.redis.set("orbit:owner:" + tid, json.dumps(owner), ex=40)
            if task["kind"] != "research":
                await self.redis.set("orbit:lease:" + task["device_id"], json.dumps(owner), ex=40)
            task["status"], task["fence"], task["worker_id"] = "running", fence, worker
            await self.save_task(task)
            return {"task": task, "fence": fence}

    async def owned(self, tid, worker, fence):
        task = await self.task(tid)
        await self.active_session(task["session_id"])
        if task["status"] in TERMINAL:
            raise Conflict("Task is no longer active")
        owner = await self.redis.get("orbit:owner:" + tid)
        if not owner or json.loads(owner) != {"worker_id": worker, "fence": fence, "task_id": tid}:
            raise Conflict("Worker ownership expired or fence is stale")
        if task["kind"] != "research":
            if await self.redis.get("orbit:lease:" + task["device_id"]) != owner:
                raise Conflict("Device lease lost")
        return task

    async def heartbeat(self, tid, worker, fence):
        async with self.transaction():
            task = await self.owned(tid, worker, fence)
            await self.redis.expire("orbit:owner:" + tid, 40)
            if task["kind"] != "research":
                await self.redis.expire("orbit:lease:" + task["device_id"], 40)
            return task

    async def event(self, tid, worker, fence, status=None, progress=None, result=None, error=None, phase=None):
        async with self.transaction():
            task = await self.owned(tid, worker, fence)
            if task["kind"] == "goal" and status == "completed" and not (result or {}).get("evidence"):
                raise ValueError("Goal completion requires evidence")
            if phase is not None:
                task["phase"] = phase
            if progress is not None:
                task["progress"] = progress
            if result is not None:
                task["result"] = result
            if status:
                await self._end(task, status, error)
            else:
                await self.save_task(task)
            return task

    async def dispatch(self, task, action, approved):
        action["status"] = "pending"
        action["expires_at"] = time.time() + 25
        await self.write("orbit:action:" + action["action_id"], action)
        await self.emit(task["device_id"], {
            "type": "device.command", "session_id": task["session_id"], "task_id": task["task_id"],
            "action_id": action["action_id"], "fence": task["fence"], "expires_at": action["expires_at"],
            "action": action["action"], "approved": approved,
        })
        await self.redis.publish("orbit:result:" + action["action_id"], json.dumps(action))

    async def action(self, tid, worker, fence, aid, action, summary, risk=None):
        needs_approval = validate_action(action)
        if is_read(action.get("type")):
            needs_approval = False
        elif risk and risk.get("level") == "high":
            needs_approval = True
        host = None
        async with self.transaction():
            task = await self.owned(tid, worker, fence)
            if task["kind"] == "research":
                raise Conflict("Research workers cannot control the desktop")
            previous = await self.redis.get("orbit:action:" + aid)
            if previous:
                previous = json.loads(previous)
                if previous["task_id"] != tid or previous["action"] != action:
                    raise Conflict("Action ID cannot be reused with a different payload")
                return previous
            if task.get("pending_action"):
                current = await self.read("orbit:action:" + task["pending_action"])
                if current["status"] in {"pending", "needs_confirmation"}:
                    raise Conflict("Only one action can be outstanding")
            record = {"action_id": aid, "task_id": tid, "action": action, "summary": summary,
                      "status": "needs_confirmation" if needs_approval else "pending", "risk": risk}
            task["pending_action"] = aid
            if needs_approval:
                record["summary"] = (approval_summary(action)
                                     + "\n\nExact action JSON:\n"
                                     + json.dumps(action, ensure_ascii=False, indent=2))
                task["status"] = "needs_confirmation"
                await self.write("orbit:action:" + aid, record)
                await self.save_task(task)
                frame = {"type": "confirmation", "task_id": tid,
                         "session_id": task["session_id"], "action_id": aid,
                         "version": task["version"], "summary": record["summary"]}
                payload = card_payload(action)
                if payload is not None:
                    frame["payload"] = payload
                await self.emit(task["device_id"], frame)
            elif is_host(action.get("type")):
                record["expires_at"] = time.time() + 25
                await self.write("orbit:action:" + aid, record)
                await self.save_task(task)
                host = record
            else:
                await self.save_task(task)
                await self.dispatch(task, record, False)
            if host is None:
                return record
        return await self._run_host(host)

    async def action_result(self, tid, worker, fence, aid):
        await self.owned(tid, worker, fence)
        action = await self.read("orbit:action:" + aid)
        if action["task_id"] != tid:
            raise Conflict("Action belongs to another task")
        if action["status"] == "pending" and action["expires_at"] < time.time():
            # Never replay an action with an uncertain device result.
            await self.cancel(tid, "Device action timed out; inspect before starting a new task")
            raise Conflict("Device response timed out")
        return action

    async def confirm(self, sid, tid, aid, version, approved):
        host = None
        async with self.transaction():
            task = await self.task(tid)
            await self.owned(tid, task.get("worker_id"), task.get("fence"))
            if (task["session_id"] != sid or task["status"] != "needs_confirmation"
                    or task["version"] != version or task.get("pending_action") != aid):
                raise Conflict("Approval no longer matches the pending action")
            action = await self.read("orbit:action:" + aid)
            if not approved:
                action["status"], action["error"] = "failed", "User declined"
                await self.write("orbit:action:" + aid, action)
                await self._end(task, "cancelled", "User declined action")
                await self.redis.publish("orbit:result:" + aid, json.dumps(action))
                return
            task["status"] = "running"
            if is_host(action["action"].get("type")):
                action["status"] = "pending"
                action["expires_at"] = time.time() + 25
                await self.write("orbit:action:" + aid, action)
                await self.save_task(task)
                host = action
            else:
                await self.save_task(task)
                await self.dispatch(task, action, True)
        if host:
            await self._run_host(host)

    async def _run_host(self, action):
        try:
            if self.effects is None:
                raise RuntimeError("Host effect is not configured")
            task = await self.task(action["task_id"])
            if task["status"] in TERMINAL:
                raise RuntimeError("Task is no longer active")
            result = await self.effects(action["action"])
            error = None
            ok = True
        except Exception as error:
            result, ok, error = {}, False, str(error)[:300] or "Room call failed"
        await self._store_host_result(action, ok, result, error)
        return await self.read("orbit:action:" + action["action_id"])

    async def _store_host_result(self, action, ok, result, error):
        aid = action["action_id"]
        async with self.transaction():
            task = await self.task(action["task_id"])
            current = await self.read("orbit:action:" + aid)
            if (current["status"] != "pending" or task.get("pending_action") != aid
                    or current["expires_at"] < time.time()):
                raise Conflict("Late or unmatched room result")
            current["status"] = "completed" if ok else "failed"
            current["error"] = error
            current["result"] = {key: value for key, value in result.items() if key in PERSISTED_RESULT_KEYS}
            await self.write("orbit:action:" + aid, current)
            await self.redis.publish("orbit:result:" + aid, json.dumps(current))

    async def device_result(self, sid, tid, aid, fence, ok, result, error=None):
        async with self.transaction():
            task = await self.task(tid)
            await self.owned(tid, task.get("worker_id"), fence)
            action = await self.read("orbit:action:" + aid)
            if (task["session_id"] != sid or action["task_id"] != tid
                    or task.get("pending_action") != aid or action["status"] != "pending"
                    or action["expires_at"] < time.time()):
                raise Conflict("Late or unmatched device result")
            action["status"] = "completed" if ok else "failed"
            transient = dict(action, result=result, error=error)
            # Screenshots travel only through Pub/Sub to the waiting HTTP request;
            # Redis persistence (AOF/RDB) must never contain their pixels.
            action["result"] = {key: value for key, value in result.items() if key in PERSISTED_RESULT_KEYS}
            action["error"] = error
            await self.write("orbit:action:" + aid, action)
            await self.redis.publish("orbit:result:" + aid, json.dumps(transient))

    async def artifact(self, tid, aid, filename, media_type):
        uuid.UUID(aid)
        if Path(filename).name != filename or media_type not in {"application/pdf", "text/markdown"}:
            raise ValueError("Invalid artifact metadata")
        path = self.artifacts / aid
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 20_000_000:
            raise ValueError("Artifact must be a contained file below 20 MB")
        task = await self.task(tid)
        if task["status"] in TERMINAL:
            raise Conflict("Task is no longer active")
        value = dict(task_id=tid, artifact_id=aid, filename=filename, media_type=media_type)
        await self.write("orbit:artifact:" + aid, value)
        return value

    async def get_artifact(self, aid):
        uuid.UUID(aid)
        return await self.read("orbit:artifact:" + aid)

    async def memories(self):
        ordered = await self.redis.lrange("orbit:memory:order", 0, 199)
        records = []
        for mid in ordered:
            raw = await self.redis.get("orbit:memory:" + mid)
            if raw:
                records.append(json.loads(raw))
        return records

    async def backfill_embeddings(self, records, cap=20):
        """Lazy Redis-side embed fill for recall; not used on plain list."""
        missing = [item for item in records if not item.get("embedding")][:cap]
        if not missing:
            return
        vectors = await embed_texts([item["text"] for item in missing])
        for item, vector in zip(missing, vectors):
            if vector is None:
                continue
            item["embedding"] = vector
            await self.redis.set("orbit:memory:" + item["id"], json.dumps(item))

    async def remember(self, text, kind, source_turn):
        if kind not in {"preference", "episode"} or not isinstance(text, str):
            raise ValueError("A memory is one preference or episode sentence")
        text = " ".join(text.split())
        if not text or len(text) > 240:
            raise ValueError("A memory is one preference or episode sentence")
        record = {"id": str(uuid.uuid4()), "text": text, "kind": kind,
                  "created_at": time.time(), "source_turn": source_turn or ""}
        vectors = await embed_texts([record["text"]])
        if vectors and vectors[0] is not None:
            record["embedding"] = vectors[0]
        await self.redis.set("orbit:memory:" + record["id"], json.dumps(record))
        await self.redis.lpush("orbit:memory:order", record["id"])
        overflow = await self.redis.lrange("orbit:memory:order", 200, -1)
        if overflow:
            await self.redis.ltrim("orbit:memory:order", 0, 199)
            for old in overflow:
                await self.redis.delete("orbit:memory:" + old)
        return record

    async def forget(self, mid):
        if not isinstance(mid, str) or not mid:
            raise ValueError("Missing memory")
        await self.redis.delete("orbit:memory:" + mid)
        await self.redis.lrem("orbit:memory:order", 0, mid)
        return {"id": mid, "deleted": True}
