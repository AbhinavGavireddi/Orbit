from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize('filename', ['compose.yaml', 'compose.prod.yaml'])
def test_task_authority_reaches_decision_over_container_network(filename):
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / filename).read_text())
    environment = config['services']['task']['environment']
    assert environment.get('ORBIT_DECISION_URL') == 'http://decision:8104'
    assert environment.get('ORBIT_ROOM_URL') == 'http://room:8105'
    ports = config['services']['room'].get('ports') or []
    assert all(str(port).startswith('127.0.0.1:') for port in ports)
