"""Live Docker voice gateway check; synthetic input and no speaker or desktop actions.

Uses one persistent session for four general questions and a protocol interruption.
Runtime credential loading only. Results are network/coordination metrics, not
microphone-to-speaker latency or proof of acoustic interruption.
"""
import asyncio
from array import array
from contextlib import suppress
from datetime import datetime, timezone
import json
import time
import uuid

import httpx
from websockets.asyncio.client import connect

from benchmark_voice import ROOT, load_pcm
from orbit_common.config import Settings
from orbit_common.evaluation import duration_summary


class ProbeSettings(Settings):
    orbit_voice_url: str = 'ws://127.0.0.1:8101'


class GatewayProbe:
    def __init__(self, socket):
        self.socket = socket
        self.ready = asyncio.Event()
        self.done = asyncio.Event()
        self.audio = asyncio.Event()
        self.cleared = asyncio.Event()
        self.first_audio = None
        self.clear_at = None
        self.transcribed = False
        self.errors = []

    async def receive(self):
        async for raw in self.socket:
            if isinstance(raw, bytes):
                if self.first_audio is None and any(abs(x) >= 200 for x in array('h', raw)):
                    self.first_audio = time.monotonic()
                    self.audio.set()
                continue
            frame = json.loads(raw)
            kind = frame.get('type')
            if kind == 'ready':
                self.ready.set()
            elif kind == 'transcript' and frame.get('role') == 'assistant':
                self.transcribed = True
            elif kind == 'state' and frame.get('state') == 'listening' and self.transcribed:
                self.done.set()
            elif kind == 'audio.clear':
                self.clear_at = time.monotonic()
                self.cleared.set()
                await self.socket.send(json.dumps({'type': 'audio.cleared',
                    'interruption_id': frame['interruption_id'], 'positions': []}))
            elif kind == 'error':
                # Do not persist arbitrary provider response text.
                self.errors.append('gateway_error')

    async def stream(self, pcm):
        start = time.monotonic()
        offset = 0
        while True:
            chunk = pcm[offset:offset + 960].ljust(960, b'\0')
            await self.socket.send(chunk)
            offset += 960
            await asyncio.sleep(max(0, start + offset / 48000 - time.monotonic()))

    async def trial(self, identity):
        pcm, duration = load_pcm(ROOT / 'var/validation/audio' / (identity + '.wav'))
        self.done.clear()
        self.audio.clear()
        self.first_audio, self.transcribed = None, False
        start = time.monotonic()
        producer = asyncio.create_task(self.stream(pcm))
        try:
            await asyncio.wait_for(self.done.wait(), 35)
            return {'fixture_id': identity,
                    'speech_end_to_received_audio_ms': round((self.first_audio - start - duration) * 1000, 2)}
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def interrupt(self):
        self.first_audio = None
        self.audio.clear()
        await self.socket.send(json.dumps({'type': 'text', 'text': 'Please count slowly from one to twenty.'}))
        await asyncio.wait_for(self.audio.wait(), 25)
        self.cleared.clear()
        start = time.monotonic()
        await self.socket.send(json.dumps({'type': 'interrupt', 'positions': []}))
        await asyncio.wait_for(self.cleared.wait(), 5)
        return round((self.clear_at - start) * 1000, 2)


async def main():
    config = ProbeSettings(_env_file=ROOT / '.env')
    headers = {'Authorization': 'Bearer ' + config.orbit_device_token}
    report = {'measurement': 'live_gateway_no_playback', 'measured_at': datetime.now(timezone.utc).isoformat(),
              'trials': [], 'limits': 'Synthetic PCM; no microphone, speaker, desktop effects or acoustic interruption.'}
    async with httpx.AsyncClient(base_url=config.orbit_task_url, headers=headers, timeout=10) as http:
        device = 'gateway-probe-' + uuid.uuid4().hex
        response = await http.post('/v1/sessions', json={'device_id': device})
        response.raise_for_status()
        sid = response.json()['session_id']
        try:
            url = config.orbit_voice_url + '/v1/voice?session_id=' + sid + '&device_id=' + device
            async with connect(url, additional_headers=headers, max_size=4_000_000) as socket:
                probe = GatewayProbe(socket)
                reader = asyncio.create_task(probe.receive())
                try:
                    await asyncio.wait_for(probe.ready.wait(), 20)
                    for identity in ('voice-26', 'voice-27', 'voice-28', 'voice-29'):
                        row = await probe.trial(identity)
                        report['trials'].append(row)
                        print(json.dumps(row), flush=True)
                    report['interrupt_to_clear_frame_ms'] = await probe.interrupt()
                    report['errors'] = probe.errors
                finally:
                    reader.cancel()
                    with suppress(asyncio.CancelledError):
                        await reader
        except Exception as error:
            report['error_class'] = type(error).__name__
        finally:
            await http.post('/v1/sessions/' + sid + '/close')
    report['received_audio'] = duration_summary(r['speech_end_to_received_audio_ms']
        for r in report['trials'] if r['speech_end_to_received_audio_ms'] >= 0)
    output = ROOT / 'var/validation/live-gateway-20261005.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if report.get('error_class') or report.get('errors'):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
