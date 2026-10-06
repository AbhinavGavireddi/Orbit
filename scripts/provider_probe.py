"""Bounded provider diagnostics. Runtime credential loading only; no app actions.

Output contains status codes, sanitized error codes and timing, never credentials,
headers, configuration dumps or provider response bodies. The longer Jev call is
diagnostic only and does not change the production 400 ms decision deadline.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shared"), str(ROOT / "services/decision"), str(ROOT / "services/voice")]
from orbit_common.config import Settings  # noqa: E402
from orbit_decision.app import INTENT_QUESTION, parse_decision  # noqa: E402
from orbit_voice.protocol import conversation_config, transcription_config  # noqa: E402


def error_code(data):
    value = (data.get("error") or {}).get("code", "provider_error")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) else "provider_error"


async def realtime(config, transcription=False):
    url = "wss://api.openai.com/v1/realtime?" + ("intent=transcription" if transcription else urlencode({"model": config.orbit_realtime_model}))
    started = time.monotonic()
    try:
        async with asyncio.timeout(15):
            async with connect(url, additional_headers={"Authorization": "Bearer " + config.openai_api_key},
                               open_timeout=10, max_size=4_000_000) as socket:
                await socket.send(json.dumps(transcription_config(config) if transcription else conversation_config(config)))
                async for raw in socket:
                    data = json.loads(raw)
                    if data.get("type") in {"session.updated", "transcription_session.updated"}:
                        return {"status": "configuration_accepted", "elapsed_ms": round((time.monotonic()-started)*1000, 2)}
                    if data.get("type") == "error":
                        return {"status": "rejected", "error_code": error_code(data)}
    except InvalidStatus as error:
        return {"status": "rejected", "http_status": error.response.status_code}
    except Exception as error:
        return {"status": "unavailable", "error_class": type(error).__name__}
    return {"status": "disconnected"}


async def jev(config):
    observations = []
    async with httpx.AsyncClient(timeout=5, limits=httpx.Limits(keepalive_expiry=60)) as client:
        for label, timeout in [("cold_diagnostic", 5), ("warm_production_budget", .4)]:
            started = time.monotonic()
            row = {"phase": label}
            try:
                async with asyncio.timeout(timeout):
                    response = await client.post("https://api.typesafe.ai/v1/systemone",
                        headers={"Authorization": "Bearer " + config.typesafe_api_key},
                        json={"model": config.orbit_jev_model, "state": {"utterance": "Open Notes", "language": "en"},
                              "questions": {"intent": INTENT_QUESTION}})
                    row["http_status"] = response.status_code
                    response.raise_for_status()
                    decision = parse_decision(response.json(), "Open Notes", config.orbit_jev_model)
                    row.update(available=decision.available, eligible=decision.eligible, capability=decision.capability)
            except Exception as error:
                row["error_class"] = type(error).__name__
            row["elapsed_ms"] = round((time.monotonic()-started)*1000, 2)
            observations.append(row)
    return observations


async def agent(config):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.openai.com/v1/responses",
                headers={"Authorization": "Bearer " + config.openai_api_key},
                json={"model": config.orbit_agent_model, "input": "Reply with OK.", "max_output_tokens": 32})
            if response.is_success:
                return {"http_status": response.status_code, "status": response.json().get("status")}
            return {"http_status": response.status_code, "error_code": error_code(response.json())}
    except Exception as error:
        return {"status": "unavailable", "error_class": type(error).__name__}


async def main():
    config = Settings(_env_file=ROOT / ".env")
    # Independent provider diagnostics run together; none executes a desktop command.
    values = await asyncio.gather(realtime(config), realtime(config, True), jev(config), agent(config))
    report = {"measured_at": datetime.now(timezone.utc).isoformat(),
              **dict(zip(("realtime", "transcription", "jev", "agent"), values))}
    output = ROOT / "var/validation/provider-probe.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
