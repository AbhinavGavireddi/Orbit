from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .routing import CapabilityName, RouteSource


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(WireModel):
    device_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


BoundedText = Annotated[str, Field(min_length=1, max_length=1000)]
TERMINAL = frozenset({"completed", "partial", "blocked", "failed", "cancelled"})


class TaskCreate(SessionCreate):
    session_id: UUID
    kind: Literal["goal", "automation", "research", "dictation"]
    goal: str = Field(min_length=1, max_length=8000)
    constraints: list[BoundedText] = Field(default_factory=list, max_length=20)
    completion_criteria: list[BoundedText] = Field(default_factory=list, max_length=20)
    request_id: str | None = Field(default=None, min_length=1, max_length=160)
    generation: int = Field(default=0, ge=0)
    turn_id: str | None = Field(default=None, min_length=1, max_length=160)
    route_source: RouteSource | None = None
    capability: CapabilityName | None = None
    skill_name: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_route(self):
        if not self.goal.strip():
            raise ValueError("Goal cannot be blank")
        if bool(self.turn_id) != bool(self.route_source):
            raise ValueError("Routed tasks require both turn_id and route_source")
        if self.turn_id and not self.request_id:
            raise ValueError("Routed tasks require an idempotent request_id")
        return self


class MemoryCreate(WireModel):
    text: str = Field(min_length=1, max_length=240)
    kind: Literal["preference", "episode"]
    source_turn: str = Field(default="", max_length=160)


class Owner(WireModel):
    worker_id: str = Field(min_length=1, max_length=100)
    fence: int = Field(ge=1)


class Claim(WireModel):
    worker_id: str = Field(min_length=1, max_length=100)


class Action(WireModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(Owner):
    action_id: UUID
    action: Action
    summary: str = Field(min_length=1, max_length=2000)


class Event(Owner):
    status: Literal["completed", "partial", "blocked", "failed"] | None = None
    phase: Literal["planning", "observing", "acting", "verifying", "waiting"] | None = None
    progress: str | None = Field(default=None, max_length=4000)
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)


class Artifact(WireModel):
    task_id: UUID
    artifact_id: UUID
    filename: str = Field(min_length=1, max_length=200)
    media_type: Literal["application/pdf", "text/markdown"]
