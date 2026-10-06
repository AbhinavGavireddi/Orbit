"""Push-based action completion with stored-state recovery and transient observations."""
import json
import time

from .store import Conflict


TRANSIENT_RESULTS = {"screenshot", "ax_snapshot"}


class ActionResults:
    def __init__(self, store):
        self.store = store

    async def wait(self, tid, worker, fence, aid, submit=None, timeout=25):
        async with self.store.redis.pubsub() as subscription:
            channel = "orbit:result:" + aid
            await subscription.subscribe(channel, "orbit:task-signal:" + tid)
            # Subscribe first. A very fast device may complete before dispatch returns.
            action = await submit() if submit else await self.store.action_result(tid, worker, fence, aid)
            transient = action["action"]["type"] in TRANSIENT_RESULTS
            if transient and submit and action["status"] == "completed":
                raise Conflict("Observation already consumed; request a fresh inspection")
            deadline = time.monotonic() + timeout
            while action["status"] in {"pending", "needs_confirmation"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if transient:
                        await self.store.cancel(tid, "Observation transfer timed out")
                        raise Conflict("Observation transfer timed out")
                    return await self.store.action_result(tid, worker, fence, aid)
                message = await subscription.get_message(ignore_subscribe_messages=True,
                                                         timeout=min(1, remaining))
                # Periodic ownership/expiry checks are for revocation, not delivery.
                stored = await self.store.action_result(tid, worker, fence, aid)
                if message and message["channel"] == channel:
                    pushed = json.loads(message["data"])
                    if (pushed.get("action_id") == aid and pushed.get("task_id") == tid
                            and pushed.get("status") in {"completed", "failed"}):
                        return pushed if transient else stored
                if not transient:
                    action = stored
            return action


class RiskAssessor:
    """Advisory classification only. Task policy never delegates authorization."""
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def assess(self, task_id, action_id, action, goal):
        import asyncio
        import hashlib
        import httpx
        exact = json.dumps(action, sort_keys=True, separators=(',', ':'))
        result = {'task_id': task_id, 'action_id': action_id,
                  'action_digest': hashlib.sha256(exact.encode()).hexdigest(),
                  'level': 'unknown', 'reason': 'Risk could not be established; deterministic approval policy applies.'}
        # Observation transfer never needs model permission and must remain fast/private.
        if action['type'] in TRANSIENT_RESULTS or action['type'] == 'room_read':
            result.update(level='low', reason='Observation only; no effect.')
            return result
        try:
            async with asyncio.timeout(.4):
                response = await self.client.post(self.config.orbit_decision_url + '/v1/decisions',
                    headers={'Authorization': 'Bearer ' + self.config.orbit_service_token},
                    json={'state': {'question': 'risk', 'options': ['low', 'high', 'unknown'],
                        'utterance': json.dumps({'goal': goal, 'action': action})[:4000]}, 'deadline': .4}, timeout=.4)
                response.raise_for_status()
                data = response.json()
                if data.get('eligible') and data.get('model') == self.config.orbit_jev_model and data.get('choice') in {'low', 'high'}:
                    result.update(level=data['choice'], reason='Jev advisory effect classification; never authorization.')
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError):
            pass
        return result
