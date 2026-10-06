from types import SimpleNamespace

from orbit_automation import handler
from orbit_common.config import Settings


class Device:
    def __init__(self):
        self.task = {'task_id': 'a', 'kind': 'goal', 'goal': 'Arrange the desktop',
                     'constraints': ['No deletion'], 'completion_criteria': ['Layout visible']}
        self.actions = []
        self.phases = []
    async def action(self, kind, params=None, summary=None, action_id=None):
        self.actions.append((kind, params, action_id))
        return {'image_base64': 'AA==', 'width': 100, 'height': 100}
    async def progress(self, text, result=None, phase=None):
        self.phases.append(phase)


class Planner:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.states = []
    async def decide(self, state):
        self.states.append(dict(state))
        return next(self.decisions)


async def test_goal_reobserves_after_action_before_claiming_completion():
    cls = getattr(handler, 'GoalAgent', None)
    assert cls is not None, 'Generic goal execution is missing'
    device = Device()
    planner = Planner([
        {'actions': [{'type': 'click', 'x': 10, 'y': 20}], 'call_id': 'one'},
        {'outcome': 'completed', 'summary': 'Layout updated', 'evidence': 'Requested window arrangement visible'},
    ])
    result = await cls(planner, max_steps=3).run(device)
    assert result['outcome'] == 'completed'
    assert [x[0] for x in device.actions] == ['screenshot', 'computer', 'screenshot']
    assert device.actions[1][2]
    assert planner.states[-1]['task']['constraints'] == ['No deletion']


async def test_goal_missing_evidence_and_budget_exhaustion_never_report_success():
    cls = getattr(handler, 'GoalAgent', None)
    assert cls is not None
    for decisions in ([{'outcome': 'completed', 'summary': 'Done', 'evidence': ''}],
                      [{'actions': [{'type': 'click', 'x': 1, 'y': 1}], 'call_id': 'one'}]):
        result = await cls(Planner(decisions), max_steps=1).run(Device())
        assert result['outcome'] == 'blocked'


async def test_handler_routes_generic_goal_to_agent_not_legacy_shortcuts():
    device = Device()
    config = Settings(_env_file=None)
    obj = handler.AutomationHandler(config)
    async def run(job):
        assert job is device
        return {'outcome': 'blocked', 'summary': 'Clarification needed'}
    obj.goal_agent = SimpleNamespace(run=run)
    try:
        result = await obj.run(device)
        assert result['outcome'] == 'blocked'
        assert device.actions == []
    finally:
        await obj.close()
