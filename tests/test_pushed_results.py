import asyncio
import uuid

import fakeredis.aioredis
import httpx
import pytest
from fastapi.testclient import TestClient

from orbit_common.config import Settings
from orbit_task.app import create_app


@pytest.fixture
async def setup(tmp_path):
    db = fakeredis.aioredis.FakeRedis(decode_responses=True)
    config = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                      orbit_artifact_dir=tmp_path)
    app = create_app(config, db)
    store = app.state.store
    session = await store.session("mac")
    task = await store.create(session["session_id"], "mac", "automation", "Open Notes")
    claim = await store.claim(task["task_id"], "worker")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://task",
                                headers={"Authorization": "Bearer " + "s" * 40}) as client:
        yield client, store, task, {"worker_id": "worker", "fence": claim["fence"]}
    await db.aclose()


async def dispatched(store, aid):
    async with asyncio.timeout(1):
        while not await store.redis.exists("orbit:action:" + aid):
            await asyncio.sleep(0.001)


async def test_action_wait_subscribes_before_dispatch_and_returns_pushed_result(setup):
    client, store, task, owner = setup
    aid = str(uuid.uuid4())
    path = f"/v1/internal/tasks/{task['task_id']}/actions"
    pending = asyncio.create_task(client.post(path, params={"wait": True}, json={
        **owner, "action_id": aid, "action": {"type": "open_app", "params": {"bundle_id": "com.apple.Notes"}},
        "summary": "Open Notes"}))
    await dispatched(store, aid)
    await asyncio.sleep(0.01)
    assert not pending.done(), "A waiting action must not return before native completion"
    await store.device_result(task["session_id"], task["task_id"], aid, owner["fence"], True,
                              {"app_bundle_id": "com.apple.Notes"})
    response = await asyncio.wait_for(pending, 0.1)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["result"]["app_bundle_id"] == "com.apple.Notes"


async def test_ax_snapshot_returns_transient_controls_without_persisting_them(setup):
    client, store, task, owner = setup
    aid = str(uuid.uuid4())
    path = f"/v1/internal/tasks/{task['task_id']}/actions"
    pending = asyncio.create_task(client.post(path, json={
        **owner, "action_id": aid, "action": {"type": "ax_snapshot", "params": {}},
        "summary": "Read controls"}))
    await dispatched(store, aid)
    controls = [{"id": "7", "role": "AXButton", "title": "Calendar", "description": "month"}]
    await store.device_result(task["session_id"], task["task_id"], aid, owner["fence"], True,
                              {"controls": controls, "app": "com.apple.Calendar"})
    response = await asyncio.wait_for(pending, 0.2)
    assert response.status_code == 200
    assert response.json()["result"]["controls"] == controls
    stored = await store.redis.get("orbit:action:" + aid)
    assert "Calendar" not in stored
    assert '"result": {}' in stored


async def test_lost_action_notification_recovers_stored_result_without_dispatch(setup):
    client, store, task, owner = setup
    aid = str(uuid.uuid4())
    await store.action(task["task_id"], "worker", owner["fence"], aid,
                       {"type": "open_app", "params": {"bundle_id": "com.apple.Notes"}}, "Open")
    await store.device_result(task["session_id"], task["task_id"], aid, owner["fence"], True, {"ok": True})
    before = await store.redis.xlen("orbit:device:mac")
    reply = await client.get(f"/v1/internal/tasks/{task['task_id']}/actions/{aid}",
                             params={**owner, "wait": True})
    assert reply.json()["status"] == "completed"
    assert await store.redis.xlen("orbit:device:mac") == before


async def test_cancel_wakes_waiting_action_and_revokes_result(setup):
    client, store, task, owner = setup
    aid = str(uuid.uuid4())
    pending = asyncio.create_task(client.post(f"/v1/internal/tasks/{task['task_id']}/actions",
        params={"wait": True}, json={**owner, "action_id": aid,
            "action": {"type": "open_app", "params": {"bundle_id": "com.apple.Notes"}}, "summary": "Open"}))
    await dispatched(store, aid)
    await store.cancel(task["task_id"])
    assert (await asyncio.wait_for(pending, 0.2)).status_code == 409


def test_task_event_connection_starts_with_snapshot_then_live_events(tmp_path):
    config = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                      orbit_artifact_dir=tmp_path)
    with TestClient(create_app(config, fakeredis.aioredis.FakeRedis(decode_responses=True))) as client:
        device = {"Authorization": "Bearer " + "d" * 40}
        service = {"Authorization": "Bearer " + "s" * 40}
        sid = client.post("/v1/sessions", headers=device, json={"device_id": "mac"}).json()["session_id"]
        body = {"session_id": sid, "device_id": "mac", "kind": "research", "goal": "Topic"}
        first = client.post("/v1/tasks", headers=device, json=body).json()
        with client.websocket_connect(f"/v1/sessions/{sid}/events", headers=service) as ws:
            second = client.post("/v1/tasks", headers=device, json={**body, "goal": "Another"}).json()
            snapshot = ws.receive_json()
            assert snapshot["type"] == "task.snapshot"
            assert first["task_id"] in {t["task_id"] for t in snapshot["tasks"]}
            assert ws.receive_json()["type"] == "followup.snapshot"
            event = ws.receive_json()
            assert event["type"] == "task.event"
            assert event["task"]["task_id"] == second["task_id"]
