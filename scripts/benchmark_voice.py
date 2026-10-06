"""Replay identical English PCM fixtures through the production voice coordinator.

Real providers, simulated task authority/device: no desktop actions or audible
playback. These component results CANNOT promote Jev or prove native latency.
Run --generate first; macOS's installed Rishi/Tara voices synthesize the fixtures.
"""
import argparse
import asyncio
from array import array
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import wave

import fakeredis.aioredis
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shared"), *(str(ROOT / "services" / name) for name in ("voice", "task", "decision"))]
from orbit_common.config import Settings  # noqa: E402
from orbit_common.evaluation import duration_summary  # noqa: E402
from orbit_common.routing import CAPABILITIES  # noqa: E402
from orbit_decision.app import evaluate, warm_connection  # noqa: E402
from orbit_task.store import Store  # noqa: E402
from orbit_voice.app import VoiceSession  # noqa: E402


def fixtures():
    return json.loads((ROOT / "evals/voice_english.json").read_text())["fixtures"]


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for index, fixture in enumerate(fixtures()):
        path = directory / (fixture["id"] + ".wav")
        subprocess.run(["say", "-v", "Rishi" if index % 2 == 0 else "Tara", "-r", "175",
                        "--file-format=WAVE", "--data-format=LEI16@24000", "-o", str(path), fixture["text"]], check=True)
        load_pcm(path)
    write_manifest(directory)
    print("Generated 30 synthetic English WAV fixtures; no speaker playback.")


def write_manifest(directory):
    rows = []
    for fixture in fixtures():
        pcm, _ = load_pcm(directory / (fixture["id"] + ".wav"))
        rows.append({**fixture, "pcm_sha256": hashlib.sha256(pcm).hexdigest()})
    (directory / "manifest.json").write_text(json.dumps(rows, indent=2) + "\n")


def load_pcm(path):
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
            raise ValueError("Fixtures must be mono PCM16 at 24 kHz")
        raw = source.readframes(source.getnframes())
    samples = array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    last = next((i for i in range(len(samples)-1, -1, -1) if abs(samples[i]) >= 200), None)
    if last is None:
        raise ValueError("Silent fixture")
    # Preserve a 100 ms tail. Timing uses the last non-silent sample, not file end.
    return raw[:min(len(raw), (last + 2401)*2)], (last + 1) / 24000


class FixtureWire:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.ready, self.reply = asyncio.Event(), asyncio.Event()
        self.first_audio = None
        self.transcripts, self.errors = [], []

    async def send_json(self, frame):
        if frame["type"] == "ready":
            self.ready.set()
        elif frame["type"] == "transcript":
            self.transcripts.append({"role": frame["role"], "text": frame["text"]})
            if frame["role"] == "assistant":
                self.reply.set()
        elif frame["type"] == "error":
            self.errors.append(frame["message"])

    async def send_bytes(self, value):
        samples = array("h", value)
        if any(abs(sample) >= 200 for sample in samples) and self.first_audio is None:
            self.first_audio = time.monotonic()

    async def receive(self):
        return await self.queue.get()

    async def close(self):
        await self.queue.put({"type": "websocket.disconnect"})


class ComponentSession(VoiceSession):
    async def task_updates(self):
        # There is no native execution in this harness. Task acceptance must not
        # masquerade as successful completion or generate a completion reply.
        await asyncio.Event().wait()


async def stream_silence(wire):
    """A real awake microphone keeps delivering frames while endpointing waits."""
    while True:
        await wire.queue.put({"type": "websocket.receive", "bytes": bytes(960)})
        await asyncio.sleep(.02)


async def measure(fixture, pcm, speech_duration, config, jev_client):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = Store(redis, ROOT / "var/validation/component-artifacts")
    state = await store.session("fixture-device")
    wire = FixtureWire()
    accepted, decisions = [], []

    async def broker(request):
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/v1/decisions":
            result = await evaluate(body["state"], config, jev_client)
            decisions.append(result)
        elif path == "/v1/tasks":
            result = await store.create(body.pop("session_id"), body.pop("device_id"), **body)
            accepted.append({"at": time.monotonic(), "capability": result["capability"],
                             "route_source": result["route_source"], "goal": result["goal"]})
        elif path.endswith("/close"):
            await store.close(state["session_id"])
            result = {}
        else:
            result = state
        return httpx.Response(200, json=result)

    session = ComponentSession(wire, config, state["session_id"], "fixture-device")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://component-broker")
    running = asyncio.create_task(session.run())
    row = {"fixture_id": fixture["id"], "capability": fixture["expected_capability"],
           "measurement": "provider_component", "mode": config.orbit_jev_mode,
           "vad_eagerness": config.orbit_vad_eagerness, "measured_at": datetime.now(timezone.utc).isoformat(),
           "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "meaningful_reply_verified": False}
    silence = None
    speech_end = None
    try:
        async with asyncio.timeout(25):
            await wire.ready.wait()
            start = time.monotonic()
            speech_end = start + speech_duration
            # Fixed 20 ms frames, including trailing silence for semantic VAD.
            frames = pcm + bytes(24000 * 2)
            for offset in range(0, len(frames), 960):
                await wire.queue.put({"type": "websocket.receive", "bytes": frames[offset:offset+960]})
                await asyncio.sleep(max(0, start + (offset + 960) / 48000 - time.monotonic()))
            silence = asyncio.create_task(stream_silence(wire))
            await asyncio.wait_for(wire.reply.wait(), 12)
            # Tool arguments may finish after the spoken acknowledgement.
            if fixture["expected_capability"]:
                deadline = time.monotonic() + 3
                while not accepted and time.monotonic() < deadline:
                    await asyncio.sleep(.02)
    except Exception as error:
        row["error_class"] = type(error).__name__
    finally:
        if silence:
            silence.cancel()
            await asyncio.gather(silence, return_exceptions=True)
        if speech_end is not None:
            row["speech_end_to_first_received_audio_ms"] = (
                round((wire.first_audio - speech_end) * 1000, 2) if wire.first_audio else None)
            row["premature_audio"] = wire.first_audio is not None and wire.first_audio < speech_end
            row["speech_end_to_task_acceptance_ms"] = round((accepted[0]["at"] - speech_end)*1000, 2) if accepted else None
        await wire.close()
        try:
            await asyncio.wait_for(running, 3)
        except Exception as error:
            row.setdefault("error_class", type(error).__name__)
        running.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await running
        await redis.aclose()
    row.update(transcripts=wire.transcripts, errors=wire.errors, decisions=decisions,
               accepted=[{k: v for k, v in item.items() if k != "at"} for item in accepted])
    return row


async def run(args):
    config = Settings(_env_file=ROOT / ".env").model_copy(update={
        "orbit_jev_mode": args.mode, "orbit_jev_capabilities": ",".join(CAPABILITIES),
        "orbit_vad_eagerness": args.vad})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in args.output.read_text().splitlines()] if args.resume and args.output.exists() else []
    for row, fixture in zip(rows, fixtures(), strict=False):
        pcm, _ = load_pcm(args.audio / (fixture["id"] + ".wav"))
        if (row["fixture_id"] != fixture["id"] or row["mode"] != args.mode or row["vad_eagerness"] != args.vad
                or row["pcm_sha256"] != hashlib.sha256(pcm).hexdigest()):
            raise ValueError("Resume requires the same ordered fixtures, audio and configuration")
    async with httpx.AsyncClient(timeout=.4, limits=httpx.Limits(keepalive_expiry=60)) as client:
        warmup = await warm_connection(config, client) if args.mode != "off" else None
        for fixture in fixtures()[len(rows):args.limit]:
            pcm, end = load_pcm(args.audio / (fixture["id"] + ".wav"))
            row = await measure(fixture, pcm, end, config, client)
            rows.append(row)
            args.output.write_text("".join(json.dumps(item) + "\n" for item in rows))
            print(json.dumps({"fixture": fixture["id"], "audio_ms": row.get("speech_end_to_first_received_audio_ms"),
                              "task_accept_ms": row.get("speech_end_to_task_acceptance_ms"),
                              "error": row.get("error_class")}), flush=True)
            # A timeout is an observed failed trial, not a reason to silently
            # drop the rest of the fixed set. Stop only systemic auth/config failures.
            if row.get("error_class") == "InvalidStatus" or any(
                    code in error for error in row["errors"] for code in ("invalid_api_key", "model_not_found", "insufficient_quota")):
                break
    report = {"measurement": "provider_component", "mode": args.mode, "vad": args.vad,
              "n": len(rows), "connection_warmup": warmup,
              "received_audio": duration_summary(r["speech_end_to_first_received_audio_ms"] for r in rows
                  if r.get("speech_end_to_first_received_audio_ms") is not None and not r.get("premature_audio")),
              "task_acceptance": duration_summary(r["speech_end_to_task_acceptance_ms"] for r in rows
                  if r.get("speech_end_to_task_acceptance_ms") is not None and r["speech_end_to_task_acceptance_ms"] >= 0),
              "limits": "No audible playback, microphone, native actions, AEC, interruption or completion measured. No promotion."}
    args.output.with_suffix(".report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--manifest-only", action="store_true", help="Hash existing validated fixtures without resynthesizing")
    parser.add_argument("--audio", type=Path, default=ROOT / "var/validation/audio")
    parser.add_argument("--mode", choices=["off", "active"], default="off")
    parser.add_argument("--vad", choices=["medium", "high"], default="medium")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--resume", action="store_true", help="Keep existing observations; continue without retrying failed fixtures")
    parser.add_argument("--output", type=Path, default=ROOT / "var/validation/voice-baseline.jsonl")
    arguments = parser.parse_args()
    if arguments.generate:
        generate(arguments.audio)
    elif arguments.manifest_only:
        write_manifest(arguments.audio)
    else:
        asyncio.run(run(arguments))
