"""Jev is the only router: skills, memory, research, and the Accessibility channel.

Pytest collects this file. No earlier test file covers skill shortlist, memory few/skip,
or an Accessibility press that skips the screenshot model. Memory rows use synthetic
text and integer created_at values.
"""
import json

import httpx
import pytest

from orbit_automation.handler import AutomationHandler
from orbit_common.accessibility import rank_controls
from orbit_common.config import Settings
from orbit_common.memory import rank_memories, selected_episodes, standing_preferences
from orbit_common.skills import index_skills, shortlist_skills
from orbit_task.store import Store
from orbit_voice.app import VoiceSession
from test_voice import Wire


def _decision(capability):
    return {"available": True, "capability": capability, "direct": True, "confidence": .99,
            "probability": .99, "elapsed_ms": 12, "model": "jev-1.13.0"}


async def _session(handler, capabilities):
    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="active",
                                            orbit_jev_capabilities=capabilities), "session", "mac")
    session.provider = Wire()
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://broker")
    return session


async def test_paraphrased_dictation_submits_without_the_exact_regex():
    submitted = []

    async def broker(request):
        if request.url.path == "/v1/tasks":
            body = json.loads(request.content)
            submitted.append(body)
            return httpx.Response(200, json={**body, "task_id": "task", "status": "queued", "route_source": "jev"})
        return httpx.Response(200, json=_decision("start_dictation"))

    session = await _session(broker, "start_dictation")
    await session.route("please write down what I say next", "turn-1", 0)
    assert submitted[0]["capability"] == "start_dictation"
    assert submitted[0]["route_source"] == "jev"
    await session.http.aclose()


async def test_answer_on_a_question_does_not_start_a_task():
    submitted = []

    async def broker(request):
        if request.url.path == "/v1/tasks":
            submitted.append(request)
        return httpx.Response(200, json=_decision("answer"))

    session = await _session(broker, "answer,clarify,computer,skill")
    await session.route("What is the weather?", "turn-1", 0)
    assert submitted == []
    await session.http.aclose()


async def test_a_question_does_not_start_a_skill(tmp_path):
    skill = tmp_path / "notes-helper"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: notes-helper\ndescription: notes helper\n---\n")
    submitted = []

    async def broker(request):
        if request.url.path == "/v1/tasks":
            submitted.append(request)
        return httpx.Response(200, json=_decision("skill"))

    session = await _session(broker, "skill")
    session.skills = index_skills([tmp_path])
    await session.route("What is the notes helper?", "turn-1", 0)
    assert submitted == []
    await session.http.aclose()


async def test_skill_act_picks_one_shortlisted_name(tmp_path):
    for name in ["notes-helper", "calendar-helper", "mail-helper"]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name.replace('-', ' ')}\n---\n")
    submitted = []

    async def broker(request):
        body = json.loads(request.content)
        if request.url.path == "/v1/tasks":
            submitted.append(body)
            return httpx.Response(200, json={**body, "task_id": "task", "status": "queued", "route_source": "jev"})
        if body.get("state", {}).get("question") == "skill":
            assert len(body["state"]["options"]) <= 9
            assert "notes-helper" in body["state"]["options"]
            return httpx.Response(200, json={"available": True, "eligible": True, "choice": "notes-helper"})
        return httpx.Response(200, json=_decision("skill"))

    session = await _session(broker, "skill")
    session.skills = index_skills([tmp_path])
    await session.route("use the notes helper", "turn-1", 0)
    assert submitted[0]["capability"] == "skill"
    assert submitted[0]["skill_name"] == "notes-helper"
    await session.http.aclose()


def test_shortlist_keeps_eight_and_newer_duplicate_wins(tmp_path):
    older, newer = tmp_path / "old", tmp_path / "new"
    for folder in (older, newer):
        folder.mkdir()
    import os
    (older / "SKILL.md").write_text("---\nname: shared\ndescription: shared notes\n---\n")
    (newer / "SKILL.md").write_text("---\nname: shared\ndescription: shared notes newer\n---\n")
    os.utime(older / "SKILL.md", (1, 1))
    os.utime(newer / "SKILL.md", (2, 2))
    records = index_skills([older.parent])
    shared = [item for item in records if item.name == "shared"]
    assert len(shared) == 1
    assert shared[0].path == newer / "SKILL.md"
    many = []
    for index in range(12):
        folder = tmp_path / f"skill-{index}"
        folder.mkdir()
        (folder / "SKILL.md").write_text(f"---\nname: skill-{index}\ndescription: calendar notes\n---\n")
        many.extend(index_skills([folder]))
    assert len(shortlist_skills("calendar notes", many)) == 8


def test_few_injects_three_ranked_sentences_and_skip_stores_nothing():
    memories = [
        {"id": str(index), "text": f"calendar plan {index}", "kind": "episode", "created_at": index}
        for index in range(5)
    ]
    memories.append({"id": "pref", "text": "I use Chrome for music", "kind": "preference", "created_at": 9})
    ranked = rank_memories("open the calendar", memories)
    chosen = selected_episodes("few", ranked)
    assert len(chosen) == 3
    assert all(item["kind"] == "episode" for item in chosen)
    assert selected_episodes("none", ranked) == []
    assert standing_preferences(memories)[0]["text"] == "I use Chrome for music"


async def test_jev_skip_writes_no_memory_and_few_is_what_the_reply_hears():
    stored = []

    async def broker(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/memories" and request.method == "POST":
            stored.append(body)
            return httpx.Response(200, json={"id": "new"})
        if request.url.path == "/v1/memories":
            memories = [{"id": str(index), "text": f"calendar plan {index}", "kind": "episode", "created_at": index}
                        for index in range(5)]
            return httpx.Response(200, json={"memories": memories})
        if body.get("state", {}).get("question") == "recall":
            return httpx.Response(200, json={"available": True, "eligible": True, "choice": "few"})
        if body.get("state", {}).get("question") == "remember":
            return httpx.Response(200, json={"available": True, "eligible": True, "choice": "skip"})
        return httpx.Response(200, json=_decision("answer"))

    session = await _session(broker, "answer")
    memories = await session.recall("open the calendar", "turn-1", 0)
    assert len(memories["episodes"]) == 3
    await session.remember("open the calendar", "turn-1", 0)
    assert stored == []
    await session.http.aclose()


async def test_research_capability_keeps_a_long_goal(tmp_path):
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, tmp_path)
    session = await store.session("mac")
    goal = "solar " * 200
    task = await store.create(session["session_id"], "mac", "research", goal, request_id="research-1",
                              turn_id="turn-1", route_source="jev", capability="research")
    assert task["kind"] == "research"
    assert len(task["goal"]) > 500
    with pytest.raises(ValueError):
        await store.create(session["session_id"], "mac", "automation", "notes", request_id="bad",
                           turn_id="turn-2", route_source="jev", capability=None)
    await redis.aclose()


async def test_memory_cap_drops_the_oldest_sentence(tmp_path):
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, tmp_path)
    for index in range(201):
        await store.remember(f"sentence {index} about plans", "episode", "turn")
    memories = await store.memories()
    assert len(memories) == 200
    texts = {item["text"] for item in memories}
    assert "sentence 0 about plans" not in texts
    assert "sentence 200 about plans" in texts
    await redis.aclose()


class MatchingJob:
    def __init__(self, task):
        self.task, self.actions, self.params = task, [], []

    async def action(self, kind, params=None, summary=None):
        self.actions.append(kind)
        self.params.append(params or {})
        if kind == "ax_snapshot":
            return {"controls": [{"id": "7", "role": "AXButton", "title": "Calendar", "description": "month",
                     "ax_ref": {"version": 1, "app_bundle_id": "com.apple.Calendar", "role": "AXButton",
                                "title": "Calendar", "description": "month", "child_path": [0, 1]}}]}
        if kind == "ax_perform":
            return {"ok": True}
        if kind == "screenshot":
            raise AssertionError("screenshot model")
        return {"ok": True}

    async def progress(self, text, result=None):
        pass


async def test_one_matching_control_does_not_call_the_screenshot_model():
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(500)

    async def decider(goal, ranked):
        assert len(ranked) == 1
        return ranked[0]["id"]

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        handler = AutomationHandler(Settings(_env_file=None, openai_api_key="test"), client, decider=decider)
        job = MatchingJob({"kind": "automation", "goal": "Open Calendar", "capability": "computer"})
        result = await handler.run(job)
    assert calls == []
    assert job.actions == ["ax_snapshot", "ax_perform"]
    assert job.params[1]["ax_ref"]["child_path"] == [0, 1]
    assert result["verification"] == "accessibility"


async def test_learned_replay_does_not_call_sol_and_a_miss_leaves_the_file(tmp_path):
    directory = tmp_path / "learned-skills"
    directory.mkdir()
    path = directory / "open-calendar.json"
    payload = {"name": "open-calendar", "description": "Open Calendar", "steps": [
        {"type": "ax_perform", "params": {"role": "AXButton", "title": "Calendar", "action": "AXPress", "mutating": True}}]}
    path.write_text(json.dumps(payload))

    async def provider(request):
        pytest.fail("replay must not call sol")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        handler = AutomationHandler(Settings(_env_file=None, orbit_skill_roots=str(directory)), client)
        job = MatchingJob({"kind": "automation", "goal": "Open Calendar", "capability": "skill",
                           "skill_name": "open-calendar"})
        result = await handler.run(job)
    assert result["verification"] == "learned_skill"
    assert json.loads(path.read_text()) == payload

    class Miss(MatchingJob):
        async def action(self, kind, params=None, summary=None):
            self.actions.append(kind)
            if kind == "ax_perform":
                raise RuntimeError("missing")
            if kind == "ax_snapshot":
                return {"controls": []}
            if kind == "screenshot":
                return {"image_base64": "cG5n", "width": 10, "height": 10}
            return {}

    replies = [{"id": "r1", "status": "completed", "output": [{"type": "function_call", "name": "finish_task",
                "arguments": '{"success":true,"summary":"done","evidence":"window"}'}]}]

    async def fallback(request):
        return httpx.Response(200, json=replies.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(fallback)) as client:
        handler = AutomationHandler(Settings(
            _env_file=None, orbit_skill_roots=str(directory), openai_api_key="test"), client)
        job = Miss({"kind": "automation", "goal": "Open Calendar", "capability": "skill", "skill_name": "open-calendar"})
        await handler.run(job)
    assert json.loads(path.read_text()) == payload
    assert "screenshot" in job.actions


def test_rank_controls_returns_at_most_eight():
    controls = [{"role": "AXButton", "title": f"Calendar {index}", "description": ""} for index in range(12)]
    assert len(rank_controls("Calendar", controls)) == 8


def test_rank_controls_preserves_semantic_ax_ref():
    ref = {"version": 1, "role": "AXButton", "title": "Calendar", "description": "", "child_path": [1]}
    ranked = rank_controls("Calendar", [{"role": "AXButton", "title": "Calendar", "description": "", "ax_ref": ref}])
    assert ranked[0]["ax_ref"] == ref
