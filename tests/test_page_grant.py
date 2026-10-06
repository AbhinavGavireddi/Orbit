"""Page grant: empty allowlist refuses; writes wait for Confirm; worker never acts alone."""

import uuid

import fakeredis.aioredis
import pytest

from orbit_common.page import AllowingPage, check_page, host_allowed
from orbit_common.effects import Effects
from orbit_task.store import Store


def test_empty_allowlist_refuses_every_page():
    assert host_allowed("https://example.com", []) is False
    assert host_allowed("https://example.com", ["example.com"]) is True


async def test_allowing_page_blocks_disallowed_host():
    calls = []

    async def inner(action):
        calls.append(action)
        return {"url": action["params"]["url"], "title": "x", "excerpt": "y"}

    page = AllowingPage(inner, ["example.com"])
    with pytest.raises(ValueError, match="not allowed"):
        await page({"type": "browser_read", "params": {"url": "https://evil.test/"}})
    assert calls == []
    await page({"type": "browser_read", "params": {"url": "https://example.com/"}})
    assert calls


async def test_browser_act_waits_for_confirm_not_worker(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    seen = []

    async def page(action):
        seen.append(action["type"])
        return {"url": action["params"]["url"], "title": "Example", "excerpt": "Hello"}

    store = Store(redis, tmp_path, effects=Effects({"browser_read": page, "browser_act": page}))
    session = await store.session("face")
    task = await store.create(session["session_id"], "face", "goal", "Act on the page")
    claim = await store.claim(task["task_id"], "worker")
    read = check_page({"type": "browser_read", "params": {"url": "https://example.com/"}})
    assert read["url"] == "https://example.com/"
    await store.action(
        task["task_id"], "worker", claim["fence"], str(uuid.uuid4()),
        {"type": "browser_read", "params": {"url": "https://example.com/"}}, "read")
    aid = str(uuid.uuid4())
    pending = await store.action(
        task["task_id"], "worker", claim["fence"], aid,
        {"type": "browser_act", "params": {
            "url": "https://example.com/", "verb": "click", "target": "More information"}},
        "click")
    assert pending["status"] == "needs_confirmation"
    assert seen == ["browser_read"]
    version = (await store.task(task["task_id"]))["version"]
    await store.confirm(session["session_id"], task["task_id"], aid, version, True)
    done = await store.action_result(task["task_id"], "worker", claim["fence"], aid)
    assert done["status"] == "completed"
    assert seen == ["browser_read", "browser_act"]
    await redis.aclose()
