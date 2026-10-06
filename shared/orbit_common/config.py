from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="")
    orbit_device_token: str = ""
    orbit_service_token: str = ""
    openai_api_key: str = ""
    typesafe_api_key: str = ""
    redis_url: str = "redis://127.0.0.1:6379/0"
    orbit_task_url: str = "http://127.0.0.1:8100"
    orbit_decision_url: str = "http://127.0.0.1:8104"
    orbit_room_url: str = "http://127.0.0.1:8105"
    orbit_browser_hosts: str = ""
    orbit_artifact_dir: Path = Path("var/artifacts")
    orbit_realtime_model: str = "gpt-realtime-2.1-mini"
    orbit_transcribe_model: str = "gpt-4o-transcribe"
    orbit_agent_model: str = "gpt-5.6-sol"
    orbit_jev_model: str = "jev-1.13.0"
    orbit_jev_mode: Literal["off", "shadow", "active"] = "shadow"
    # Empty by default. Populate only with capabilities passing the promotion report.
    orbit_jev_capabilities: str = ""
    orbit_vad_eagerness: Literal["medium", "high"] = "medium"
    orbit_max_task_steps: int = 30
    orbit_skill_roots: str = ""
    orbit_learned_skills_dir: str = ""

    def require_auth(self):
        if len(self.orbit_device_token) < 32 or len(self.orbit_service_token) < 32:
            raise RuntimeError("Run scripts/setup.py to create distinct local authentication tokens")
        if self.orbit_device_token == self.orbit_service_token:
            raise RuntimeError("Device and service tokens must differ")
