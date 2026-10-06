"""General computer-control hard fails from the README acceptance sheet.

Automated proofs against Task contracts with a fake Redis/device.
These do not claim unlocked-Mac live GUI acceptance (open-apps / free-form trials).
"""

from __future__ import annotations

import json
import uuid

import pytest

import fakeredis.aioredis

from orbit_task.store import Conflict, Store


@pytest.fixture
async def store(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield Store(redis, tmp_path)
    await redis.aclose()


async def claimed(store, kind="automation"):
    session = await store.session("mac")
    task = await store.create(session["session_id"], "mac", kind, "general control")
    claim = await store.claim(task["task_id"], "worker-a")
    return task, claim["fence"]


CLICK = {"type": "computer", "params": {"actions": [{"type": "click", "x": 10, "y": 20, "button": "left"}]}}
TYPE = {"type": "computer", "params": {"actions": [{"type": "type", "text": "hello"}]}}


async def test_reject_approval_cancels_task_and_runs_nothing(store):
    """Sheet (3): reject approval — nothing runs."""
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    pending = await store.action(task["task_id"], "worker-a", fence, aid, CLICK, "Click target")
    assert pending["status"] == "needs_confirmation"
    version = (await store.task(task["task_id"]))["version"]
    await store.confirm(task["session_id"], task["task_id"], aid, version, False)
    state = await store.task(task["task_id"])
    assert state["status"] == "cancelled"
    action = json.loads(await store.redis.get("orbit:action:" + aid))
    assert action["status"] == "failed"
    assert action["error"] == "User declined"
    frames = [json.loads(f["json"]) for _, f in await store.redis.xrange("orbit:device:mac")]
    dispatched = [
        f for f in frames
        if f.get("type") == "device.command" and f.get("approved") is True and f.get("action_id") == aid
    ]
    assert dispatched == []


async def test_stop_mid_pending_computer_batch_rejects_late_device_result(store):
    """Sheet (5): Stop mid-batch — no late actions."""
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    await store.action(task["task_id"], "worker-a", fence, aid, CLICK, "Click target")
    version = (await store.task(task["task_id"]))["version"]
    await store.confirm(task["session_id"], task["task_id"], aid, version, True)
    await store.stop(task["session_id"])
    with pytest.raises(Conflict):
        await store.device_result(task["session_id"], task["task_id"], aid, fence, True, {"clicked": True})
    with pytest.raises(Conflict):
        await store.create(task["session_id"], "mac", "automation", "late tool", generation=0)


async def test_approval_card_actions_must_match_stored_payload(store):
    """Approval identity: same action_id cannot mutate to a different payload."""
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    await store.action(task["task_id"], "worker-a", fence, aid, CLICK, json.dumps(CLICK["params"]["actions"]))
    version = (await store.task(task["task_id"]))["version"]
    await store.confirm(task["session_id"], task["task_id"], aid, version, True)
    await store.device_result(task["session_id"], task["task_id"], aid, fence, True, {"clicked": True})
    replay = await store.action(task["task_id"], "worker-a", fence, aid, CLICK, "same")
    assert replay["status"] == "completed"
    with pytest.raises(Conflict):
        await store.action(task["task_id"], "worker-a", fence, aid, TYPE, "different batch")


async def test_stale_fence_cannot_dispatch_after_lease_move(store):
    """Ops fail: stale fence after ownership move."""
    task, fence = await claimed(store)
    other = await store.create(task["session_id"], "mac", "automation", "second goal")
    with pytest.raises(Conflict):
        await store.claim(other["task_id"], "worker-b")
    await store.cancel(task["task_id"])
    next_claim = await store.claim(other["task_id"], "worker-b")
    with pytest.raises(Conflict):
        await store.action(
            other["task_id"], "worker-b", fence, str(uuid.uuid4()), CLICK, "stale fence"
        )
    ok = await store.action(
        other["task_id"],
        "worker-b",
        next_claim["fence"],
        str(uuid.uuid4()),
        {"type": "screenshot", "params": {}},
        "fresh fence",
    )
    assert ok["status"] in {"pending", "completed", "needs_confirmation"}


async def test_uncertain_worker_recovery_fails_closed_without_blind_retry(store):
    """Sheet (8): disconnect / uncertain — fail closed, no blind retry."""
    task, _ = await claimed(store)
    await store.redis.delete("orbit:lease:mac", f"orbit:owner:{task['task_id']}")
    with pytest.raises(Conflict):
        await store.claim(task["task_id"], "worker-b")
    assert (await store.task(task["task_id"]))["status"] == "failed"


@pytest.mark.parametrize("winner", ["jev", "realtime"])
async def test_twin_realtime_and_jev_cannot_both_own_same_turn(store, winner):
    """Hard fail: twin Realtime+Jev owner on one turn_id."""
    session = await store.session("mac")
    common = dict(
        sid=session["session_id"],
        device="mac",
        kind="automation",
        goal="Open Notes",
        generation=0,
        turn_id="same-turn",
        capability="open_notes",
    )
    first = await store.create(**common, request_id="first", route_source=winner)
    loser = "jev" if winner == "realtime" else "realtime"
    second = await store.create(**common, request_id="second", route_source=loser)
    assert first["task_id"] == second["task_id"]
    assert second["route_source"] == winner
    assert await store.redis.xlen("orbit:jobs:automation") == 1


async def test_mutating_computer_batch_always_needs_confirmation(store):
    """Mutating computer batches never auto-dispatch without button approval."""
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    pending = await store.action(task["task_id"], "worker-a", fence, aid, CLICK, "click")
    assert pending["status"] == "needs_confirmation"
    frames = [json.loads(f["json"]) for _, f in await store.redis.xrange("orbit:device:mac")]
    approved = [f for f in frames if f.get("type") == "device.command" and f.get("approved") is True
                and f.get("action_id") == aid]
    assert approved == []
