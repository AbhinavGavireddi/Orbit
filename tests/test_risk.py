import httpx
import pytest
from orbit_common.config import Settings
from orbit_task import actions
from orbit_decision.app import question_for


def test_risk_question_defines_effects_not_just_labels():
    _, question = question_for({'question': 'risk', 'options': ['low', 'high', 'unknown']})
    assert 'financial' in question['criteria']['high']
    assert 'uncertain' in question['criteria']['unknown']


@pytest.mark.parametrize('payload,expected', [
    ({'eligible': True, 'choice': 'high', 'model': 'jev-1.13.0'}, 'high'),
    ({'eligible': False, 'choice': 'low'}, 'unknown'),
    ({'eligible': True, 'choice': 'low', 'model': 'wrong'}, 'unknown'),
])
async def test_risk_result_bound_to_action_and_provider_checked(payload, expected):
    cls = getattr(actions, 'RiskAssessor', None)
    assert cls is not None
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
    try:
        result = await cls(Settings(_env_file=None), client).assess('task', 'action', {'type': 'computer', 'params': {}}, 'Arrange')
        assert result['level'] == expected
        assert result['action_id'] == 'action'
        assert result['task_id'] == 'task'
    finally:
        await client.aclose()
