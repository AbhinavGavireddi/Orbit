import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / 'scripts' / (name + '.py')
    assert path.exists(), f'{name} is missing'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validation_budget_reserves_before_spending_and_keeps_uncertain_cost(tmp_path):
    cls = load('validation_budget').ValidationBudget
    budget = cls(tmp_path / 'usage.sqlite')
    budget.reserve('one', 4400)
    budget.reserve('one', 4400)
    with pytest.raises(ValueError):
        budget.reserve('two', 101)
    budget.settle('one', 100)
    budget.reserve('two', 4400)
    with pytest.raises(ValueError):
        budget.reserve('negative', -1)
    assert budget.total() == 4500


def test_release_configuration_rejects_adhoc_and_invalid_version():
    validate = load('release_mac').validate
    with pytest.raises(ValueError):
        validate('-', '0.2.0', 'profile')
    with pytest.raises(ValueError):
        validate('Developer ID Application: Example (TEAM)', '../bad', 'profile')
    assert validate('Developer ID Application: Example (TEAM)', '0.2.0', 'profile') is None


def test_beta_gate_rejects_missing_trials_and_false_success():
    check = load('check_beta').evaluate
    fixtures = [{'id': 'one', 'expected': 'verified_completion'}, {'id': 'two', 'expected': 'clarify_or_handoff'}]
    assert not check(fixtures, [])['passed']
    rows = [dict(id='one', correct=True, human_verified=True, false_success=False,
                 unapproved_actions=0, duplicate_effects=0, post_cancel_actions=0),
            dict(id='two', correct=True, human_verified=True, false_success=False,
                 unapproved_actions=0, duplicate_effects=0, post_cancel_actions=0)]
    assert check(fixtures, rows)['passed']
    rows[0]['false_success'] = True
    assert not check(fixtures, rows)['passed']
