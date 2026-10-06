import json
import uuid

import fakeredis.aioredis
import pytest

from orbit_task.store import Conflict, Store


@pytest.fixture
async def store(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield Store(redis, tmp_path)
    await redis.aclose()


async def claimed(store, kind="automation"):
    session = await store.session("mac")
    task = await store.create(session["session_id"], "mac", kind, "open Notes")
    claim = await store.claim(task["task_id"], "worker-a")
    return task, claim["fence"]


async def test_only_one_worker_owns_desktop_and_stale_fence_cannot_dispatch(store):
    task, fence = await claimed(store)
    other = await store.create(task["session_id"], "mac", "automation", "another goal")
    with pytest.raises(Conflict):
        await store.claim(other["task_id"], "worker-b")
    with pytest.raises(Conflict):
        await store.action(task["task_id"], "worker-a", fence + 1, str(uuid.uuid4()),
                           {"type": "screenshot", "params": {}}, "Inspect")
    await store.cancel(task["task_id"])
    next_claim = await store.claim(other["task_id"], "worker-b")
    assert next_claim["fence"] > fence


async def test_computer_task_keeps_the_users_words_and_rejects_an_empty_goal(store):
    session = await store.session("mac")
    task = await store.create(session["session_id"], "mac", "automation", "  Open Calendar  ",
                              request_id="computer-1", turn_id="turn-1", route_source="jev", capability="computer")
    assert task["goal"] == "Open Calendar"
    assert task["capability"] == "computer"
    with pytest.raises(ValueError, match="Computer task"):
        await store.create(session["session_id"], "mac", "automation", "   ",
                           request_id="computer-2", turn_id="turn-2", route_source="jev", capability="computer")


async def test_retried_task_submission_reuses_job_and_rejects_payload_change(store):
    session = await store.session("mac")
    args = (session["session_id"], "mac", "research", "solar cells", "client-request-1")
    first = await store.create(*args)
    retry = await store.create(*args)
    assert retry["task_id"] == first["task_id"]
    assert await store.redis.xlen("orbit:jobs:research") == 1
    with pytest.raises(Conflict):
        await store.create(*args[:3], "changed topic", args[4])


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_submission_transport_failure_cannot_orphan_idempotent_job(store, monkeypatch, lost_reply):
    session = await store.session("mac")
    original = store.redis.pipeline
    faulted = False

    def flaky_pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute
        async def fail_once(*a, **kw):
            nonlocal faulted
            if faulted:
                return await execute(*a, **kw)
            faulted = True
            if lost_reply:
                await execute(*a, **kw)
            raise ConnectionError("Connection dropped around EXEC")
        pipe.execute = fail_once
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", flaky_pipeline)
    args = (session["session_id"], "mac", "research", "topic", "retry-id")
    with pytest.raises(ConnectionError):
        await store.create(*args)
    task = await store.create(*args)
    entries = await store.redis.xrange("orbit:jobs:research")
    assert len(entries) == 1
    assert entries[0][1]["task_id"] == task["task_id"]


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_terminal_commit_cannot_lose_event_or_cancellation_wakeup(store, monkeypatch, lost_reply):
    task, fence = await claimed(store)
    original_pipeline, original_xadd = store.redis.pipeline, store.redis.xadd
    tid = task["task_id"]

    def is_terminal(fields):
        frame = json.loads(fields.get("json", "{}"))
        return frame.get("type") == "task.event" and frame["task"]["status"] == "completed"

    async def failed_publication(name, fields, **kwargs):
        if is_terminal(fields):
            if lost_reply:
                await original_xadd(name, fields, **kwargs)
            raise ConnectionError("Transport failed during terminal event publication")
        return await original_xadd(name, fields, **kwargs)

    def failed_transaction(*args, **kwargs):
        pipe = original_pipeline(*args, **kwargs)
        execute = pipe.execute
        async def fail(*a, **kw):
            if any(command[0][0] == "XADD" and '"status": "completed"' in str(command[0])
                   for command in pipe.command_stack):
                if lost_reply:
                    await execute(*a, **kw)
                raise ConnectionError("Transport failed around terminal EXEC")
            return await execute(*a, **kw)
        pipe.execute = fail
        return pipe

    async with store.redis.pubsub() as subscriber:
        await subscriber.subscribe("orbit:task-signal:" + tid)
        await subscriber.get_message(timeout=1)
        with monkeypatch.context() as fault:
            fault.setattr(store.redis, "xadd", failed_publication)
            fault.setattr(store.redis, "pipeline", failed_transaction)
            with pytest.raises(ConnectionError):
                await store.event(tid, "worker-a", fence, status="completed", result={"ok": True})

        current = await store.task(tid)
        assert current["status"] == ("completed" if lost_reply else "running")
        if lost_reply:
            # Cleanup sees an already terminal task. Its event and wakeup must
            # therefore have committed even when the EXEC reply was lost.
            await store.cancel(tid)
        else:
            assert await store.redis.exists("orbit:owner:" + tid)
            await store.event(tid, "worker-a", fence, status="completed", result={"ok": True})
        frames = [json.loads(fields["json"]) for _, fields in await store.redis.xrange("orbit:device:mac")]
        terminal = [frame["task"] for frame in frames if frame["type"] == "task.event"
                    and frame["task"]["status"] == "completed"]
        assert len(terminal) == 1
        assert terminal[0] == await store.task(tid)
        assert terminal[0]["version"] == 3
        assert len([frame for frame in frames if frame["type"] == "device.cancel" and frame["task_id"] == tid]) == 1
        assert (await subscriber.get_message(ignore_subscribe_messages=True, timeout=0.2))["data"] == "changed"
        assert not await store.redis.exists("orbit:owner:" + tid, "orbit:lease:mac")


async def test_generic_click_needs_exact_approval_and_cannot_be_replayed(store):
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    action = {"type": "computer", "params": {"actions": [{"type": "click", "x": 4, "y": 5}]}}
    pending = await store.action(task["task_id"], "worker-a", fence, aid, action, "Harmless; approve this")
    assert pending["status"] == "needs_confirmation"
    assert pending["summary"].startswith("Review summary:\n- Computer-control batch with 1 step.")
    assert "Harmless; approve this" not in pending["summary"]
    assert "- Step 1: click at (4, 5)." in pending["summary"]
    assert "\n\nExact action JSON:\n" + json.dumps(action, ensure_ascii=False, indent=2) in pending["summary"]
    state = await store.task(task["task_id"])
    with pytest.raises(Conflict):
        await store.confirm(task["session_id"], task["task_id"], aid, state["version"] - 1, True)
    await store.confirm(task["session_id"], task["task_id"], aid, state["version"], True)
    with pytest.raises(Conflict):
        await store.confirm(task["session_id"], task["task_id"], aid, state["version"], True)
    await store.device_result(task["session_id"], task["task_id"], aid, fence, True, {"clicked": True})
    replay = await store.action(task["task_id"], "worker-a", fence, aid, action, "Click Play")
    assert replay["status"] == "completed"
    assert replay["result"] == {"clicked": True}
    with pytest.raises(Conflict):
        await store.action(task["task_id"], "worker-a", fence, aid,
                           {"type": "screenshot", "params": {}}, "different payload")


async def test_goodbye_revokes_pending_approval_and_late_results(store):
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    await store.action(task["task_id"], "worker-a", fence, aid,
                       {"type": "screenshot", "params": {}}, "Inspect")
    await store.close(task["session_id"])
    assert (await store.task(task["task_id"]))["status"] == "cancelled"
    with pytest.raises(Conflict):
        await store.device_result(task["session_id"], task["task_id"], aid, fence, True, {})
    with pytest.raises(Conflict):
        await store.create(task["session_id"], "mac", "research", "old session")


async def test_stop_generation_rejects_late_submissions_without_ending_conversation(store):
    task, _ = await claimed(store)
    state = await store.stop(task["session_id"])
    assert state["generation"] == 1
    with pytest.raises(Conflict):
        await store.create(task["session_id"], "mac", "research", "late old tool", generation=0)
    next_task = await store.create(task["session_id"], "mac", "research", "new explicit request", generation=1)
    assert next_task["status"] == "queued"


async def test_screenshot_bytes_are_published_transiently_never_persisted(store):
    task, fence = await claimed(store)
    aid = str(uuid.uuid4())
    await store.action(task["task_id"], "worker-a", fence, aid, {"type": "screenshot", "params": {}}, "Inspect")
    await store.device_result(task["session_id"], task["task_id"], aid, fence, True,
                              {"image_base64": "PRIVATE_SCREENSHOT", "width": 100, "height": 100})
    stored = await store.redis.get("orbit:action:" + aid)
    assert "PRIVATE_SCREENSHOT" not in stored
    assert '"width": 100' in stored


async def test_recovery_of_expired_worker_never_repeats_uncertain_actions(store):
    task, _ = await claimed(store)
    await store.redis.delete("orbit:lease:mac", f"orbit:owner:{task['task_id']}")
    with pytest.raises(Conflict):
        await store.claim(task["task_id"], "worker-b")
    assert (await store.task(task["task_id"]))["status"] == "failed"


async def test_invalid_native_action_rejected_before_queueing(store):
    task, fence = await claimed(store)
    for action in [
        {"type": "shell", "params": {"command": "rm"}},
        {"type": "open_url", "params": {"url": "file:///etc/passwd"}},
        {"type": "open_app", "params": {"bundle_id": "com.apple.Terminal"}},
    ]:
        with pytest.raises(ValueError):
            await store.action(task["task_id"], "worker-a", fence, str(uuid.uuid4()), action, "Unsafe")


async def test_ax_perform_accepts_semantic_ref_and_rejects_mismatches(store):
    task, fence = await claimed(store)
    ref = {"version": 1, "app_bundle_id": "com.apple.Calendar", "role": "AXButton",
           "title": "", "description": "Calendar", "child_path": [0, 2]}
    action = {"type": "ax_perform", "params": {"role": "AXButton", "title": "Calendar",
              "description": "Calendar", "ax_ref": ref, "action": "AXPress", "mutating": True}}
    pending = await store.action(task["task_id"], "worker-a", fence, str(uuid.uuid4()), action, "AX")
    assert pending["status"] == "needs_confirmation"
    assert "Accessibility action: AXPress on AXButton named \"Calendar\"" in pending["summary"]

    bad_ref = {**ref, "role": "AXLink"}
    with pytest.raises(ValueError, match="role mismatch"):
        await store.action(task["task_id"], "worker-a", fence, str(uuid.uuid4()),
                           {"type": "ax_perform", "params": {**action["params"], "ax_ref": bad_ref}}, "bad")
    bad_path = {**ref, "child_path": [0, 99]}
    with pytest.raises(ValueError, match="Invalid Accessibility path"):
        await store.action(task["task_id"], "worker-a", fence, str(uuid.uuid4()),
                           {"type": "ax_perform", "params": {**action["params"], "ax_ref": bad_path}}, "bad")
    with pytest.raises(ValueError, match="supported role and label"):
        await store.action(task["task_id"], "worker-a", fence, str(uuid.uuid4()),
                           {"type": "ax_perform", "params": {"role": "AXButton", "title": ""}}, "bad")


async def test_artifacts_require_existing_contained_file_and_session_authority(store):
    task, fence = await claimed(store, "research")
    aid = str(uuid.uuid4())
    with pytest.raises(ValueError):
        await store.artifact(task["task_id"], aid, "report.pdf", "application/pdf")
    (store.artifacts / aid).write_bytes(b"%PDF-demo")
    await store.artifact(task["task_id"], aid, "report.pdf", "application/pdf")
    assert (await store.get_artifact(aid))["task_id"] == task["task_id"]
    with pytest.raises(ValueError):
        await store.artifact(task["task_id"], "../../etc/passwd", "report.pdf", "application/pdf")
