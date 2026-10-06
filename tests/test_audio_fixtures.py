import asyncio
import importlib.util
from pathlib import Path
import wave

import pytest


spec = importlib.util.spec_from_file_location("voice_benchmark", Path(__file__).parents[1] / "scripts/benchmark_voice.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_empty_synthesis_cannot_be_reported_as_audio_evidence(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
    with pytest.raises(ValueError, match="Silent fixture"):
        benchmark.load_pcm(path)


async def test_endpoint_wait_keeps_delivering_mic_frames_until_cancelled():
    wire = benchmark.FixtureWire()
    producer = asyncio.create_task(benchmark.stream_silence(wire))
    try:
        frames = [await asyncio.wait_for(wire.queue.get(), .2) for _ in range(3)]
        assert all(frame["bytes"] == bytes(960) for frame in frames)
        assert not producer.done()
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
