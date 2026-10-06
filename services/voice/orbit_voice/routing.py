"""The decision provider is never a prerequisite for a Realtime response."""
import asyncio

import httpx

from orbit_common.routing import RouteDecision


class DecisionClient:
    def __init__(self, config, http):
        self.config, self.http = config, http

    async def evaluate(self, transcript, deadline=1.5):
        try:
            async with asyncio.timeout(deadline):
                response = await self.http.post(self.config.orbit_decision_url + "/v1/decisions",
                    json={"state": {"utterance": transcript[:4000], "language": "en"}, "deadline": deadline},
                    timeout=deadline)
                response.raise_for_status()
                return RouteDecision.model_validate(response.json())
        except (TimeoutError, httpx.HTTPError, ValueError):
            return RouteDecision(error="Decision deadline or provider unavailable")

    async def choose(self, transcript, question, options, deadline=1.5):
        try:
            async with asyncio.timeout(deadline):
                response = await self.http.post(self.config.orbit_decision_url + "/v1/decisions", json={
                    "state": {"utterance": transcript[:4000], "language": "en",
                              "question": question, "options": options},
                    "deadline": deadline}, timeout=deadline)
                response.raise_for_status()
                payload = response.json()
                if payload.get("eligible") and payload.get("choice") in options:
                    return payload["choice"]
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        return None
