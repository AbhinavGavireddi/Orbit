import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts")]
from host import device_config, missing_names, service_environment, write_device_config  # noqa: E402


PARENT = {
    "ORBIT_DEVICE_TOKEN": "d" * 32,
    "ORBIT_SERVICE_TOKEN": "s" * 32,
    "OPENAI_API_KEY": "sk-test-key",
    "TYPESAFE_API_KEY": "jev-test-key",
    "TMPDIR": "/tmp",
}


def test_missing_names_lists_names_without_values():
    assert missing_names(PARENT) == []
    broken = {**PARENT, "OPENAI_API_KEY": "", "ORBIT_SERVICE_TOKEN": PARENT["ORBIT_DEVICE_TOKEN"]}
    message = ", ".join(missing_names(broken))
    assert "OPENAI_API_KEY" in message
    assert "distinct service token" in message
    assert "sk-test-key" not in message
    assert PARENT["ORBIT_DEVICE_TOKEN"] not in message


def test_device_config_keeps_provider_keys_out(tmp_path):
    path = tmp_path / "config.json"
    write_device_config(path, PARENT["ORBIT_DEVICE_TOKEN"])
    saved = json.loads(path.read_text())
    assert saved == device_config(PARENT["ORBIT_DEVICE_TOKEN"])
    assert set(saved) == {"ORBIT_DEVICE_TOKEN", "ORBIT_TASK_URL", "ORBIT_VOICE_URL"}
    assert "sk-test-key" not in path.read_text()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_each_service_receives_only_the_key_it_calls(tmp_path):
    home, runtime, site, data = tmp_path / "home", tmp_path / "runtime", tmp_path / "site", tmp_path / "data"
    decision = service_environment(PARENT, "decision", runtime, site, data, home)
    voice = service_environment(PARENT, "voice", runtime, site, data, home)
    task = service_environment(PARENT, "task", runtime, site, data, home)
    assert "OPENAI_API_KEY" not in decision
    assert "TYPESAFE_API_KEY" not in voice
    assert decision["TYPESAFE_API_KEY"] == "jev-test-key"
    assert voice["OPENAI_API_KEY"] == "sk-test-key"
    assert voice["ORBIT_JEV_MODE"] == "off"
    assert "OPENAI_API_KEY" not in task
    assert "TYPESAFE_API_KEY" not in task


def test_host_starts_without_optional_jev(tmp_path):
    parent = {key: value for key, value in PARENT.items() if key != 'TYPESAFE_API_KEY'}
    assert missing_names(parent) == []
    env = service_environment(parent, 'decision', tmp_path, tmp_path, tmp_path, tmp_path)
    assert env.get('TYPESAFE_API_KEY', '') == ''
