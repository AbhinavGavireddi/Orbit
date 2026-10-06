"""Turn ownership is committed atomically with task publication by Store."""
import hashlib
import json


def scoped_key(namespace, sid, generation, identity):
    digest = hashlib.sha256(json.dumps([sid, generation, identity]).encode()).hexdigest()
    return f"orbit:{namespace}:{digest}"


class TurnCoordinator:
    def __init__(self, redis, ttl):
        self.redis, self.ttl = redis, ttl

    async def owner(self, key):
        value = await self.redis.get(key) if key else None
        return json.loads(value) if value else None

    @staticmethod
    def existing_task(owner, source):
        if owner and (source != owner["source"] or source == "jev"):
            return owner["task_id"]
        return None

    def commit(self, pipe, key, source, task_id):
        pipe.set(key, json.dumps({"source": source, "task_id": task_id}), ex=self.ttl)
