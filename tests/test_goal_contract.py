import uuid

import fakeredis.aioredis
import pytest
from pydantic import ValidationError

from orbit_common.contracts import TaskCreate
from orbit_task.policy import validate_action
from orbit_task.store import Conflict, Store


def test_goal_contract_accepts_constraints_and_completion_criteria():
    task = TaskCreate(device_id='mac', session_id=uuid.uuid4(), kind='goal', goal='Organize this window',
                      constraints=['Do not delete anything'], completion_criteria=['Requested layout visible'])
    assert task.constraints == ['Do not delete anything']
    with pytest.raises(ValidationError):
        TaskCreate(device_id='mac', session_id=uuid.uuid4(), kind='goal', goal='   ')


async def test_goal_idempotency_includes_constraints_and_stale_generation(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, tmp_path)
    try:
        session = await store.session('mac')
        args = dict(sid=session['session_id'], device='mac', kind='goal', goal='Arrange this window',
                    request_id='one', turn_id='turn', route_source='realtime',
                    constraints=['No deletion'], completion_criteria=['Layout visible'])
        first = await store.create(**args)
        assert (await store.create(**args))['task_id'] == first['task_id']
        assert await redis.xlen('orbit:jobs:automation') == 1
        with pytest.raises(Conflict):
            await store.create(**{**args, 'constraints': []})
        claim = await store.claim(first['task_id'], 'worker')
        with pytest.raises(ValueError, match='evidence'):
            await store.event(first['task_id'], 'worker', claim['fence'], status='completed', result={'summary': 'Done'})
        result = await store.event(first['task_id'], 'worker', claim['fence'], status='blocked',
                                   result={'summary': 'Needs login'})
        assert result['status'] == 'blocked'
        assert not await redis.exists('orbit:lease:mac')
        await store.stop(session['session_id'])
        with pytest.raises(Conflict):
            await store.create(**args)
    finally:
        await redis.aclose()


def test_worker_cannot_waive_accessibility_approval():
    assert validate_action({'type': 'ax_perform', 'params': {
        'role': 'AXButton', 'title': 'Submit', 'action': 'AXPress', 'mutating': False}})
