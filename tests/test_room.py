import uuid
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from fastapi.testclient import TestClient

from orbit_automation.room import room_command, run_room
from orbit_common.config import Settings
from orbit_room.app import create_app as create_room
from orbit_task.app import create_app
from orbit_task.policy import approval_summary, validate_action
from orbit_task.room import HttpRoom
from orbit_task.store import Store


def test_room_call_needs_confirm_and_names_the_payload():
    action = {"type": "room_call", "params": {"device": "lamp", "power": "on"}}
    assert validate_action(action) is True
    assert "lamp.on" in approval_summary(action)
    assert validate_action({"type": "room_read", "params": {"device": "lamp"}}) is False
    fan = {"type": "room_call", "params": {"device": "fan", "power": "off"}}
    assert validate_action(fan) is True
    assert "fan.off" in approval_summary(fan)
    printer = {"type": "room_call", "params": {"device": "printer", "job": "the note"}}
    assert validate_action(printer) is True
    assert "printer:the note" in approval_summary(printer)
    assert validate_action({"type": "room_read", "params": {"device": "climate"}}) is False
    with pytest.raises(ValueError):
        validate_action({"type": "room_call", "params": {"device": "lamp", "power": "on", "url": "http://evil"}})
    with pytest.raises(ValueError):
        validate_action({"type": "room_call", "params": {"device": "climate", "power": "on"}})


def test_room_sentences_name_one_device():
    assert room_command("Turn the lamp on") == {"device": "lamp", "power": "on"}
    assert room_command("  lamp off ") == {"device": "lamp", "power": "off"}
    assert room_command("Turn the fan on") == {"device": "fan", "power": "on"}
    assert room_command("Print the note") == {"device": "printer", "job": "the note"}
    assert room_command("Read the climate") == {"device": "climate", "read_only": True}
    assert room_command("yes") is None
    assert room_command("Open Safari") is None


class Job:
    def __init__(self, readings):
        self.task = {"task_id": "job", "kind": "goal", "goal": "Turn the lamp on"}
        self.readings = list(readings)
        self.actions = []

    async def progress(self, text, result=None, phase=None):
        return None

    async def action(self, kind, params=None, summary=None, action_id=None):
        self.actions.append(kind)
        if kind == "room_read":
            return {"device": "lamp", "power": self.readings.pop(0)}
        return {"device": "lamp", "power": params["power"]}


async def test_room_goal_reads_again_after_the_call():
    job = Job(["off", "on"])
    result = await run_room(job, {"device": "lamp", "power": "on"})
    assert result["outcome"] == "completed"
    assert result["evidence"]
    assert job.actions == ["room_read", "room_call", "room_read"]


async def test_climate_read_does_not_call():
    class Climate:
        def __init__(self):
            self.task = {"task_id": "job", "kind": "goal", "goal": "Read the climate"}
            self.actions = []

        async def progress(self, text, result=None, phase=None):
            return None

        async def action(self, kind, params=None, summary=None, action_id=None):
            self.actions.append(kind)
            return {"device": "climate", "temperature": 24, "humidity": 50}

    job = Climate()
    result = await run_room(job, {"device": "climate", "read_only": True})
    assert result["outcome"] == "completed"
    assert "24" in result["evidence"]
    assert job.actions == ["room_read"]


async def test_confirm_calls_the_lamp_and_talk_does_not(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    calls = []

    async def room(action):
        calls.append(action["type"])
        power = "off" if action["type"] == "room_read" else action["params"]["power"]
        return {"device": "lamp", "power": power}

    store = Store(redis, tmp_path, room=room)
    session = await store.session("face")
    task = await store.create(session["session_id"], "face", "goal", "Turn the lamp on",
                              constraints=["Call only the lamp"],
                              completion_criteria=["A fresh lamp reading matches the call"])
    claim = await store.claim(task["task_id"], "worker")
    aid = str(uuid.uuid4())
    pending = await store.action(task["task_id"], "worker", claim["fence"], aid,
                                 {"type": "room_call", "params": {"device": "lamp", "power": "on"}},
                                 "Allow this call?")
    assert pending["status"] == "needs_confirmation"
    assert calls == []
    current = await store.task(task["task_id"])
    await store.confirm(session["session_id"], task["task_id"], aid, current["version"], False)
    assert calls == []
    assert (await store.task(task["task_id"]))["status"] == "cancelled"

    session = await store.session("face")
    task = await store.create(session["session_id"], "face", "goal", "Turn the lamp on")
    claim = await store.claim(task["task_id"], "worker")
    reading = await store.action(task["task_id"], "worker", claim["fence"], str(uuid.uuid4()),
                                 {"type": "room_read", "params": {"device": "lamp"}}, "Read the lamp")
    assert reading["result"]["power"] == "off"
    aid = str(uuid.uuid4())
    await store.action(task["task_id"], "worker", claim["fence"], aid,
                       {"type": "room_call", "params": {"device": "lamp", "power": "on"}},
                       "Allow this call?")
    current = await store.task(task["task_id"])
    await store.confirm(session["session_id"], task["task_id"], aid, current["version"], True)
    done = await store.action_result(task["task_id"], "worker", claim["fence"], aid)
    assert done["status"] == "completed"
    assert done["result"]["power"] == "on"
    assert calls == ["room_read", "room_call"]
    await redis.aclose()


def test_device_token_cannot_change_the_lamp(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                        orbit_artifact_dir=tmp_path)
    with TestClient(create_room(settings)) as client:
        device = {"Authorization": "Bearer " + "d" * 40}
        service = {"Authorization": "Bearer " + "s" * 40}
        assert client.get("/v1/lamp", headers=device).status_code == 401
        assert client.post("/v1/lamp", headers=device, json={"power": "on"}).status_code == 401
        assert client.post("/v1/devices/fan", headers=device, json={"power": "on"}).status_code == 401
        assert client.post("/v1/lamp", headers=service, json={"power": "on"}).json()["power"] == "on"
        assert client.get("/v1/lamp", headers=service).json()["power"] == "on"
        assert client.get("/v1/devices/climate", headers=service).json()["temperature"] == 24


def test_face_keeps_talk_and_confirm_apart():
    page = Path(__file__).resolve().parents[1].joinpath("services/task/orbit_task/face.html").read_text()
    assert 'id="talk"' in page and 'id="confirm"' in page and 'id="schedule"' in page
    assert page.count("approved: true") == 1
    assert "approved: false" in page
    assert '{ type: "text", text }' in page
    assert "/followups/" in page
    assert "/v1/artifacts/" in page
    assert "audio.played" in page
    assert "source.stop" in page
    assert "blockedItem" in page
    assert "ignoreUntilClear" not in page
    assert 'if (phase === "sent") return' not in page
    assert "getUserMedia" in page
    assert ":8101" in page
    assert "followup.due" in page
    assert '{ type: "ping" }' in page
    assert "device.result" in page
    assert "This client has no desktop." in page


async def test_http_room_posts_then_checks_the_reading(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                        orbit_artifact_dir=tmp_path)
    app = create_room(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://room") as client:
        room = HttpRoom("http://room", "s" * 40, client)
        assert (await room({"type": "room_read", "params": {"device": "lamp"}}))["power"] == "off"
        assert (await room({"type": "room_call", "params": {"device": "lamp", "power": "on"}}))["power"] == "on"
        assert (await room({"type": "room_call", "params": {"device": "fan", "power": "on"}}))["power"] == "on"
        printed = await room({"type": "room_call", "params": {"device": "printer", "job": "the note"}})
        assert printed["job"] == "the note"
        assert (await room({"type": "room_read", "params": {"device": "climate"}}))["temperature"] == 24


def test_face_ticket_opens_the_socket_once(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                        orbit_artifact_dir=tmp_path)
    with TestClient(create_app(settings, fakeredis.aioredis.FakeRedis(decode_responses=True))) as client:
        device = {"Authorization": "Bearer " + "d" * 40}
        session = client.post("/v1/sessions", json={"device_id": "face"}, headers=device).json()
        issued = client.post(f"/v1/sessions/{session['session_id']}/face-ticket", headers=device).json()
        ticket = issued["ticket"]
        assert issued["voice_ticket"] and issued["voice_ticket"] != ticket
        with client.websocket_connect(f"/v1/devices/face/ws?session_id={session['session_id']}&ticket={ticket}") as ws:
            assert ws.receive_json()["type"] == "connected"
        with pytest.raises(Exception):
            with client.websocket_connect(f"/v1/devices/face/ws?session_id={session['session_id']}&ticket={ticket}"):
                pass
