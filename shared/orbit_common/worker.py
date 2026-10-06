"""Infrastructure shared by workers; workflow logic stays in each service."""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import FastAPI, HTTPException
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from orbit_common.contracts import TERMINAL

log = logging.getLogger("orbit.worker")


class TaskBroker:
    def __init__(self, config, client=None):
        self.http = client or httpx.AsyncClient(base_url=config.orbit_task_url, timeout=8,
            headers={"Authorization": "Bearer " + config.orbit_service_token})

    async def call(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()


@dataclass
class Job:
    task: dict
    worker_id: str
    fence: int
    broker: TaskBroker

    @property
    def path(self):
        return "/v1/internal/tasks/" + self.task["task_id"]

    @property
    def owner(self):
        return {"worker_id": self.worker_id, "fence": self.fence}

    async def progress(self, text, result=None, phase=None):
        return await self.broker.call("POST", self.path + "/event", json={
            **self.owner, "progress": text, "result": result, "phase": phase})

    async def action(self, kind, params=None, summary=None, action_id=None):
        # One ID survives push waits/recovery; never mint a new ID for uncertain outcomes.
        aid = action_id or str(uuid.uuid4())
        body = {**self.owner, "action_id": aid, "action": {"type": kind, "params": params or {}},
                "summary": summary or kind.replace("_", " ")}
        deadline = time.monotonic() + 180
        response = await self.broker.call("POST", self.path + "/actions", json=body,
                                          params={"wait": True}, timeout=30)
        while response["status"] in {"pending", "needs_confirmation"}:
            if time.monotonic() > deadline:
                raise TimeoutError("Approval/device response timed out")
            response = await self.broker.call("GET", self.path + "/actions/" + aid,
                params={**self.owner, "wait": True}, timeout=min(30, deadline - time.monotonic()))
        if response["status"] != "completed":
            raise RuntimeError(response.get("error") or "Native action failed")
        return response.get("result", {})


class JobHandler(Protocol):
    async def run(self, job: Job) -> dict: ...


class Worker:
    def __init__(self, config, queue: str, handler: JobHandler):
        self.config, self.queue, self.handler = config, queue, handler
        self.worker_id = queue + "-" + uuid.uuid4().hex
        self.redis = Redis.from_url(config.redis_url, decode_responses=True)
        self.broker = TaskBroker(config)
        self.stopping = asyncio.Event()
        self.connected = False

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(10)
            await self.broker.call("POST", job.path + "/heartbeat", json=job.owner)

    async def execute(self, task_id):
        while not self.stopping.is_set():
            try:
                claim = await self.broker.call("POST", f"/v1/internal/tasks/{task_id}/claim",
                                                json={"worker_id": self.worker_id})
                break
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {404, 409}:
                    raise
                task = await self.broker.call("GET", "/v1/tasks/" + task_id)
                if task["status"] in TERMINAL:
                    return
                # Busy desktop or an active lease: wait without replaying anything.
                await asyncio.sleep(1)
        else:
            return
        job = Job(claim["task"], self.worker_id, claim["fence"], self.broker)
        run = asyncio.create_task(self.handler.run(job))
        pulse = asyncio.create_task(self.heartbeat(job))
        try:
            done, _ = await asyncio.wait([run, pulse], return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                pulse.result()
                raise RuntimeError("Ownership lost")
            result = run.result()
            await self.broker.call("POST", job.path + "/event", json={**job.owner, "status": result.get("outcome", "completed"), "result": result})
        except asyncio.CancelledError:
            with suppress(httpx.HTTPError):
                await self.broker.call("POST", "/v1/tasks/" + task_id + "/cancel")
            raise
        except Exception as error:
            log.warning("task_failed task_id=%s class=%s", task_id, type(error).__name__)
            try:
                await self.broker.call("POST", job.path + "/event", json={**job.owner, "status": "failed",
                    "error": str(error)[:300] if isinstance(error, (RuntimeError, ValueError, TimeoutError))
                             else "Provider or device unavailable; inspect state before retry"})
            except httpx.HTTPError:
                with suppress(httpx.HTTPError):
                    await self.broker.call("POST", "/v1/tasks/" + task_id + "/cancel")
        finally:
            run.cancel()
            pulse.cancel()
            await asyncio.gather(run, pulse, return_exceptions=True)

    async def run(self):
        stream = "orbit:jobs:" + self.queue
        while not self.stopping.is_set():
            try:
                try:
                    await self.redis.xgroup_create(stream, "workers", id="0", mkstream=True)
                except ResponseError as error:
                    if "BUSYGROUP" not in str(error):
                        raise
                self.connected = True
                recovered = await self.redis.xautoclaim(stream, "workers", self.worker_id, 60000, count=1)
                messages = recovered[1]
                if not messages:
                    rows = await self.redis.xreadgroup("workers", self.worker_id, {stream: ">"}, count=1, block=1000)
                    messages = rows[0][1] if rows else []
                for mid, payload in messages:
                    await self.execute(payload["task_id"])
                    await self.redis.xack(stream, "workers", mid)
                    await self.redis.xdel(stream, mid)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.connected = False
                log.warning("worker_retry class=%s", type(error).__name__)
                await asyncio.sleep(2)

    async def close(self):
        self.stopping.set()
        await self.redis.aclose()
        await self.broker.http.aclose()


def worker_app(config, queue, handler):
    worker = Worker(config, queue, handler)

    @asynccontextmanager
    async def lifespan(app):
        config.require_auth()
        runner = asyncio.create_task(worker.run())
        yield
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await worker.close()
        if hasattr(handler, "close"):
            await handler.close()

    app = FastAPI(title="Orbit " + queue + " Worker", lifespan=lifespan)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": queue}

    @app.get("/readyz")
    async def ready():
        if not worker.connected or not config.openai_api_key:
            raise HTTPException(503, "Worker requires Redis, task API and OPENAI_API_KEY")
        try:
            await worker.redis.ping()
            await worker.broker.call("GET", "/readyz", timeout=2)
        except Exception:
            raise HTTPException(503, "Worker dependency unavailable")
        return {"ready": True}

    return app
