import asyncio

import fakeredis.aioredis
import pytest

from orbit_task.store import Conflict, Store


@pytest.fixture
async def store(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield Store(redis, tmp_path)
    await redis.aclose()


@pytest.mark.parametrize("winner", ["jev", "realtime"])
async def test_one_turn_has_one_route_and_one_effect(store, winner):
    session = await store.session("mac")
    common = dict(sid=session["session_id"], device="mac", kind="automation", goal="Open Notes",
                  generation=0, turn_id="input-1", capability="open_notes")
    first = await store.create(**common, request_id="first", route_source=winner)
    loser = "jev" if winner == "realtime" else "realtime"
    second = await store.create(**common, request_id="second", route_source=loser)
    assert first["task_id"] == second["task_id"]
    assert second["route_source"] == winner
    assert await store.redis.xlen("orbit:jobs:automation") == 1


async def test_concurrent_route_proposals_publish_exactly_one_job(store):
    session = await store.session("mac")
    common = dict(sid=session["session_id"], device="mac", kind="automation", goal="Open Chrome",
                  generation=0, turn_id="input-race", capability="open_chrome")
    replies = await asyncio.gather(*[
        store.create(**common, request_id=f"{source}-{i}", route_source=source)
        for i, source in enumerate(["jev", "realtime", "jev", "jev"])
    ])
    assert len({task["task_id"] for task in replies}) == 1
    assert await store.redis.xlen("orbit:jobs:automation") == 1


async def test_realtime_winner_preserves_compound_tasks_and_each_call_is_retry_safe(store):
    session = await store.session("mac")
    common = dict(sid=session["session_id"], device="mac", turn_id="compound", route_source="realtime")
    first = await store.create(**common, kind="automation", goal="Open Notes", request_id="call-1")
    second = await store.create(**common, kind="research", goal="Research batteries", request_id="call-2")
    retry = await store.create(**common, kind="research", goal="Research batteries", request_id="call-2")
    assert first["task_id"] != second["task_id"] == retry["task_id"]
    assert await store.redis.xlen("orbit:jobs:research") == 1
    late = await store.create(**{**common, "route_source": "jev"}, kind="automation", goal="Open Notes",
                              request_id="jev", capability="open_notes")
    assert late["task_id"] == first["task_id"]


async def test_stop_revokes_late_jev_and_new_generation_has_independent_turn_identity(store):
    session = await store.session("mac")
    common = dict(sid=session["session_id"], device="mac", kind="automation", goal="Open Notes",
                  request_id="same-id", turn_id="same-turn", route_source="jev", capability="open_notes")
    first = await store.create(**common)
    await store.stop(session["session_id"])
    with pytest.raises(Conflict):
        await store.create(**common)
    newer = await store.create(**common, generation=1)
    assert newer["task_id"] != first["task_id"]
    assert newer["generation"] == 1


async def test_claim_and_job_publication_survive_uncertain_transaction_reply(store, monkeypatch):
    session = await store.session("mac")
    original = store.redis.pipeline
    faulted = False

    def flaky(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        async def fail_once(*a, **kw):
            nonlocal faulted
            result = await execute(*a, **kw)
            if not faulted:
                faulted = True
                raise ConnectionError("Lost EXEC reply")
            return result
        pipe.execute = fail_once
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", flaky)
    common = dict(sid=session["session_id"], device="mac", kind="automation", goal="Open Notes",
                  request_id="jev", turn_id="input-1", route_source="jev", capability="open_notes")
    with pytest.raises(ConnectionError):
        await store.create(**common)
    recovered = await store.create(**common)
    losing = await store.create(**{**common, "route_source": "realtime", "request_id": "call-1"})
    assert recovered["task_id"] == losing["task_id"]
    assert await store.redis.xlen("orbit:jobs:automation") == 1


async def test_jev_cannot_submit_unbounded_tasks(store):
    session = await store.session("mac")
    with pytest.raises(ValueError):
        await store.create(session["session_id"], "mac", "research", "Do something",
                           "jev", turn_id="input-1", route_source="jev")
