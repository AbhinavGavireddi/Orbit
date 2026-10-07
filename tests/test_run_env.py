import importlib.util
from pathlib import Path


def load_run_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run.py"
    spec = importlib.util.spec_from_file_location("orbit_run_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_runner_scopes_provider_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    run = load_run_module()
    values = {"OPENAI_API_KEY": "openai-test", "TYPESAFE_API_KEY": "jev-test",
              "ORBIT_DEVICE_TOKEN": "d" * 40, "ORBIT_SERVICE_TOKEN": "s" * 40}
    assert run.service_env("task", values)["OPENAI_API_KEY"] == "openai-test"
    assert "TYPESAFE_API_KEY" not in run.service_env("task", values)
    assert run.service_env("task", values)["ORBIT_EMBEDDING_MODEL"] == "text-embedding-3-small"
    assert run.service_env("voice", values)["OPENAI_API_KEY"] == "openai-test"
    assert "TYPESAFE_API_KEY" not in run.service_env("voice", values)
    assert run.service_env("automation", values)["OPENAI_API_KEY"] == "openai-test"
    assert run.service_env("research", values)["OPENAI_API_KEY"] == "openai-test"
    assert run.service_env("decision", values)["TYPESAFE_API_KEY"] == "jev-test"
    assert "OPENAI_API_KEY" not in run.service_env("decision", values)
