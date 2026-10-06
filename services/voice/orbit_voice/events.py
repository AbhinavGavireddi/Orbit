"""Reconnectable task notifications; every connection begins with a snapshot."""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


class TaskEvents:
    def __init__(self, config, session_id, connector=connect):
        self.url = config.orbit_task_url.replace("http", "ws", 1) + f"/v1/sessions/{session_id}/events"
        self.headers = {"Authorization": "Bearer " + config.orbit_service_token}
        self.connector = connector

    async def frames(self):
        backoff = .1
        while True:
            try:
                async with self.connector(self.url, additional_headers=self.headers,
                                          open_timeout=5, ping_interval=20, max_size=4_000_000) as socket:
                    backoff = .1
                    async for raw in socket:
                        frame = json.loads(raw)
                        yield frame
                        if frame.get("type") == "session.closed":
                            return
            except ConnectionClosed as error:
                if error.rcvd and error.rcvd.code in {4401, 4409}:
                    raise RuntimeError("Task session no longer available") from None
            except InvalidStatus as error:
                if error.response.status_code in {401, 403, 404}:
                    raise RuntimeError("Task events authentication/session rejected") from None
            except (OSError, TimeoutError):
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2)


class Engagement:
    """Pure scheduling policy; task state stays authoritative in the task service."""
    important = {'completed', 'partial', 'blocked', 'failed', 'needs_confirmation'}

    def __init__(self, generation=0):
        self.generation = generation
        self.routine = True
        self.tasks = {}
        self.pending = {}
        self.started = {}
        self.last_spoken = float('-inf')

    def reset(self, generation):
        self.generation = generation
        self.tasks.clear()
        self.pending.clear()
        self.started.clear()

    def update(self, task, now, snapshot=False):
        if task.get('generation', 0) != self.generation:
            return
        tid = task['task_id']
        old = self.tasks.get(tid)
        if old and old['version'] >= task['version']:
            return
        self.tasks[tid] = task
        self.started.setdefault(tid, now)
        if snapshot or task['status'] == 'cancelled':
            self.pending.pop(tid, None)
            return
        changed = not old or (task['status'], task.get('phase'), task.get('progress')) != (
            old['status'], old.get('phase'), old.get('progress'))
        if changed:
            self.pending[tid] = task

    def next(self, now):
        for tid, task in sorted(self.pending.items(), key=lambda pair: pair[1]['status'] not in self.important):
            important = task['status'] in self.important
            if not important and (not self.routine or now - self.started[tid] < 4 or now - self.last_spoken < 15):
                continue
            self.pending.pop(tid)
            self.last_spoken = now
            return task
        return None
