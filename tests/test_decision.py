import asyncio
import httpx
import pytest

from orbit_decision.app import evaluate, warm_connection
from orbit_common.config import Settings


async def test_shadow_decision_outage_is_unavailable_not_permission_to_act():
    async def unavailable(request):
        return httpx.Response(529, json={"error": "overloaded"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        result = await evaluate({"utterance": "delete files"}, Settings(_env_file=None, typesafe_api_key="test"), client)
    assert result["mode"] == "shadow"
    assert result["available"] is False
    assert "decision" not in result


async def test_execution_deadline_overrides_a_short_client_timeout():
    seen = []

    class Probe:
        async def post(self, url, **kwargs):
            seen.append(kwargs.get("timeout"))
            return httpx.Response(200, json=answer(choice="start_dictation"),
                                  request=httpx.Request("POST", url))

    result = await evaluate({"utterance": "Open Notes"}, Settings(_env_file=None, typesafe_api_key="fake"),
                            Probe(), deadline=1.5)
    assert seen == [1.5]
    assert result["eligible"] is True
    assert result["capability"] == "start_dictation"


async def test_shadow_timeout_is_bounded():
    async def slow(request):
        await asyncio.sleep(3)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        result = await evaluate({}, Settings(_env_file=None, typesafe_api_key="test"), client, deadline=0.01)
    assert result["available"] is False
    assert result["elapsed_ms"] < 250


def answer(choice="open_notes", confidence=.99, probability=.99, model="jev-1.13.0"):
    from orbit_decision.app import INTENT_QUESTION
    keys = list(INTENT_QUESTION["criteria"])
    rest = (1 - probability) / (len(keys) - 1)
    probabilities = {key: (probability if key == choice else rest) for key in keys}
    return {"model": model, "answers": {"intent": {"type": "choice", "choice": choice,
            "probabilities": probabilities, "confidence": confidence}}}


@pytest.mark.parametrize("status", [401, 429, 529])
async def test_foreground_provider_error_is_not_retried(status):
    calls = []
    async def provider(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "unavailable"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await evaluate({"utterance": "Open Notes"}, Settings(_env_file=None, typesafe_api_key="fake"), client)
    assert not result["available"]
    assert len(calls) == 1


@pytest.mark.parametrize(("utterance", "choice", "confidence", "probability", "eligible"), [
    ("Launch Google Chrome", "open_chrome", .99, .99, True),
    ("Open Notes", "start_dictation", .99, .99, True),
    ("Open Calendar", "computer", .99, .99, True),
    ("Open the browser and play Spotify", "computer", .99, .99, True),
    ("Don't open Notes", "computer", .99, .99, False),
    ('He said "Open Notes"', "computer", .99, .99, False),
    ("Open Calendar", "computer", .8, .99, True),
    ("Open Calendar", "computer", .79, .99, False),
    ("Open Calendar", "computer", .99, .94, False),
    ("What is on my calendar?", "computer", .99, .99, False),
    ("please write down what I say next", "start_dictation", .99, .99, True),
    ("What is the weather?", "answer", .99, .99, True),
    ("How do I use the writing skill?", "skill", .99, .99, False),
    ("Write a report on solar cells", "research", .99, .99, True),
])
async def test_jev_is_a_scored_proposal_not_permission(utterance, choice, confidence, probability, eligible):
    async def provider(request):
        return httpx.Response(200, json=answer(choice=choice, confidence=confidence, probability=probability))
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await evaluate({"utterance": utterance}, Settings(_env_file=None, typesafe_api_key="fake"), client)
    assert result["available"]
    assert result["eligible"] is eligible
    assert result["mode"] == "shadow"
    assert result["capability"] == choice


@pytest.mark.parametrize("broken", [answer(model="unknown"), answer(probability=2),
                                  {"answers": {"intent": {"choice": "open_notes"}}}])
async def test_incomplete_or_wrong_model_decisions_fail_closed(broken):
    async def provider(request):
        return httpx.Response(200, json=broken)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await evaluate({"utterance": "Open Notes"}, Settings(_env_file=None, typesafe_api_key="fake"), client)
    assert not result["available"]


async def test_connection_warmup_is_read_only_and_does_not_relax_decision_deadline():
    calls = []
    async def provider(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            await asyncio.sleep(.02)
            return httpx.Response(200, json={"models": []})
        await asyncio.sleep(.05)
        return httpx.Response(200, json=answer())
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        config = Settings(_env_file=None, typesafe_api_key="fake")
        assert (await warm_connection(config, client))["ready"]
        result = await evaluate({"utterance": "Open Notes"}, config, client, deadline=.01)
    assert not result["available"]
    assert calls == [("GET", "/v1/models"), ("POST", "/v1/systemone")]


async def test_warmup_failure_has_no_provider_body_or_retry():
    calls = []
    async def provider(request):
        calls.append(request)
        return httpx.Response(401, text="private provider detail")
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await warm_connection(Settings(_env_file=None, typesafe_api_key="fake"), client)
    assert result["ready"] is False
    assert result["error"] == "Jev HTTP 401"
    assert "private" not in str(result)
    assert len(calls) == 1
