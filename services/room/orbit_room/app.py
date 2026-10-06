"""Local room devices. The browser cannot call this service. Task authority calls it."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orbit_common.config import Settings
from orbit_common.room_devices import ACTUATORS, SENSORS
from orbit_common.security import authorized


class PowerCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    power: str = Field(pattern=r"^(on|off)$")


class JobCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: str = Field(min_length=1, max_length=200)


def create_app(settings=None):
    config = settings or Settings()
    state = {
        "lamp": {"device": "lamp", "power": "off"},
        "fan": {"device": "fan", "power": "off"},
        "printer": {"device": "printer", "job": ""},
        "climate": {"device": "climate", "temperature": 24, "humidity": 50},
    }

    @asynccontextmanager
    async def lifespan(app):
        config.require_auth()
        yield

    app = FastAPI(title="Orbit Room", version="1.0.0", lifespan=lifespan)

    async def service(authorization: str | None = Header(default=None)):
        if not authorized(authorization, config.orbit_service_token):
            raise HTTPException(401, "Service authentication required")

    def reading(name):
        if name not in state:
            raise HTTPException(404, "Unknown room device")
        return dict(state[name])

    def apply(name, payload):
        field = ACTUATORS.get(name)
        if not field:
            raise HTTPException(404, "Unknown room device")
        model = PowerCall if field == "power" else JobCall
        try:
            call = model.model_validate(payload)
        except ValidationError as error:
            raise HTTPException(422, "Room call is not valid") from error
        state[name][field] = getattr(call, field)
        return dict(state[name])

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "room"}

    @app.get("/readyz")
    async def ready():
        return {"ready": True}

    @app.get("/v1/lamp", dependencies=[Depends(service)])
    async def read_lamp():
        return reading("lamp")

    @app.post("/v1/lamp", dependencies=[Depends(service)])
    async def write_lamp(body: PowerCall):
        return apply("lamp", body.model_dump())

    @app.get("/v1/devices/{name}", dependencies=[Depends(service)])
    async def read_device(name: str):
        if name not in ACTUATORS and name not in SENSORS:
            raise HTTPException(404, "Unknown room device")
        return reading(name)

    @app.post("/v1/devices/{name}", dependencies=[Depends(service)])
    async def write_device(name: str, payload: dict):
        return apply(name, payload)

    return app
