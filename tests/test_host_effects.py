"""One host-effect door. A page read is an observation. A click waits for Confirm."""

import uuid

import fakeredis.aioredis
import pytest

from orbit_common.effects import Effects
from orbit_common.mcp_door import check_mcp
from orbit_common.page import check_page
from orbit_automation.browser import page_url, run_page
from orbit_task.policy import approval_summary, validate_action
from orbit_task.store import Store


def test_page_read_is_an_observation_and_a_click_needs_confirm():
    read = {"type": "browser_read", "params": {"url": "https://example.com/notes"}}
    assert check_page(read)["url"] == "https://example.com/notes"
    assert validate_action(read) is False
    click = {"type": "browser_act", "params": {
        "url": "https://example.com/notes", "verb": "click", "target": "Save"}}
    assert validate_action(click) is True
    summary = approval_summary(click)
    assert "https://example.com/notes" in summary
    assert "Save" in summary
    with pytest.raises(ValueError):
        check_page({"type": "browser_read", "params": {"url": "http://example.com"}})
    with pytest.raises(ValueError):
        check_page({"type": "browser_act", "params": {
            "url": "https://example.com", "verb": "buy", "target": "Pay"}})


def test_mcp_read_is_an_observation_and_a_write_needs_confirm():
    read = {"type": "mcp_read", "params": {"server": "notes", "tool": "list", "arguments": {}}}
    assert check_mcp(read)["tool"] == "list"
    assert validate_action(read) is False
    write = {"type": "mcp_call", "params": {
        "server": "notes", "tool": "create", "arguments": {"title": "Thursday"}}}
    assert validate_action(write) is True
    summary = approval_summary(write)
    assert "notes" in summary and "create" in summary
    with pytest.raises(ValueError):
        check_mcp({"type": "mcp_call", "params": {"server": "notes", "tool": "sh", "arguments": "rm"}})


async def test_store_runs_a_page_through_effects_and_not_the_face(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    seen = []

    async def page(action):
        seen.append(action["type"])
        return {"url": action["params"]["url"], "title": "Notes", "excerpt": "Thursday"}

    store = Store(redis, tmp_path, effects=Effects({"browser_read": page, "browser_act": page}))
    session = await store.session("face")
    task = await store.create(session["session_id"], "face", "goal", "Read the page")
    claim = await store.claim(task["task_id"], "worker")
    reading = await store.action(
        task["task_id"], "worker", claim["fence"], str(uuid.uuid4()),
        {"type": "browser_read", "params": {"url": "https://example.com/notes"}}, "Read the page")
    assert reading["status"] == "completed"
    assert reading["result"]["title"] == "Notes"
    aid = str(uuid.uuid4())
    pending = await store.action(
        task["task_id"], "worker", claim["fence"], aid,
        {"type": "browser_act", "params": {
            "url": "https://example.com/notes", "verb": "click", "target": "Save"}},
        "Click Save")
    assert pending["status"] == "needs_confirmation"
    assert seen == ["browser_read"]
    current = await store.task(task["task_id"])
    await store.confirm(session["session_id"], task["task_id"], aid, current["version"], True)
    done = await store.action_result(task["task_id"], "worker", claim["fence"], aid)
    assert done["status"] == "completed"
    assert seen == ["browser_read", "browser_act"]
    await redis.aclose()


def test_one_https_address_is_a_page_read():
    assert page_url("Read https://example.com/notes.") == "https://example.com/notes"
    assert page_url("Read https://a.example and https://b.example") is None
    assert page_url("Turn the lamp on") is None


async def test_page_runner_reads_and_does_not_click():
    class Job:
        def __init__(self):
            self.calls = []

        async def progress(self, text, phase=None):
            return None

        async def action(self, kind, params=None, summary=None):
            self.calls.append(kind)
            return {"url": params["url"], "title": "Notes", "excerpt": "Thursday"}

    job = Job()
    done = await run_page(job, "https://example.com/notes")
    assert job.calls == ["browser_read"]
    assert done["verification"] == "fresh page reading"
    assert done["evidence"] == "Thursday"
