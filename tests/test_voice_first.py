import asyncio
import json

import httpx
from orbit_common.config import Settings
from orbit_voice.app import VoiceSession
from test_voice import Wire


async def session_with_broker(broker):
    session = VoiceSession(Wire(), Settings(_env_file=None), '00000000-0000-0000-0000-000000000001', 'mac')
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url='http://broker')
    return session


async def cleanup(session):
    for task in list(session.background) + list(session.recall_tasks.values()):
        task.cancel()
    await asyncio.gather(*session.background, *session.recall_tasks.values(), return_exceptions=True)
    await session.http.aclose()


async def test_committed_audio_starts_response_before_transcription_or_memory():
    session = await session_with_broker(lambda request: httpx.Response(200, json={'memories': []}))
    session.provider = Wire([
        {'type': 'input_audio_buffer.speech_started', 'item_id': 'one'},
        {'type': 'input_audio_buffer.speech_stopped', 'item_id': 'one'},
        {'type': 'input_audio_buffer.committed', 'item_id': 'one'},
    ])
    try:
        await session.receive_provider()
        responses = [x for x in session.provider.sent if x['type'] == 'response.create']
        assert len(responses) == 1
        assert responses[0]['response']['metadata']['orbit_turn_id'] == 'one'
    finally:
        await cleanup(session)


async def test_goal_tool_submits_once_without_jev_and_rejects_old_turn():
    submitted = []
    async def broker(request):
        submitted.append(json.loads(request.content))
        return httpx.Response(202, json={**submitted[-1], 'task_id': 'task', 'status': 'queued', 'version': 1})
    session = await session_with_broker(broker)
    session.provider = Wire()
    session.accept_turn('one')
    event = {'call_id': 'tool', 'name': 'submit_task', 'arguments': json.dumps({
        'goal': 'Arrange the desktop', 'constraints': [], 'completion_criteria': ['Layout visible']})}
    try:
        await session.tool(event, 0, 'one')
        await session.tool({**event, 'call_id': 'retry'}, 0, 'one')
        session.accept_turn('two')
        await session.tool({**event, 'call_id': 'late'}, 0, 'one')
        assert len(submitted) == 1
        assert submitted[0]['kind'] == 'goal'
        assert submitted[0]['route_source'] == 'realtime'
    finally:
        await cleanup(session)


async def test_slow_recall_does_not_block_typed_speech_or_create_second_reply():
    blocked = asyncio.Event()
    async def broker(request):
        await blocked.wait()
        return httpx.Response(200, json={'memories': []})
    session = await session_with_broker(broker)
    session.provider = Wire()
    session.accept_turn('one')
    work = asyncio.create_task(session.speak_after_recall('Hello Orbit', 'one', 0))
    try:
        await asyncio.wait_for(asyncio.shield(work), .1)
        assert any(x['type'] == 'response.create' for x in session.provider.sent)
        blocked.set()
        await asyncio.gather(*session.background)
        assert len([x for x in session.provider.sent if x['type'] == 'response.create']) == 1
    finally:
        blocked.set()
        await work
        await cleanup(session)


async def test_late_same_generation_response_cannot_speak_over_new_turn():
    session = await session_with_broker(lambda r: httpx.Response(200, json={}))
    session.accept_turn('old')
    session.accept_turn('new')
    session.provider = Wire([
        {'type': 'response.created', 'response': {'id': 'late', 'metadata': {'orbit_generation': '0', 'orbit_turn_id': 'old'}}},
        {'type': 'response.output_audio.delta', 'response_id': 'late', 'item_id': 'audio', 'delta': 'AQI='},
    ])
    try:
        await session.receive_provider()
        assert b'\x01\x02' not in session.ws.sent
        assert 'late' in session.cancelled_responses
    finally:
        await cleanup(session)


async def test_progress_waits_for_user_speech_and_native_playback():
    session = await session_with_broker(lambda r: httpx.Response(200, json={}))
    session.provider = Wire()
    try:
        await session.task_update({'task_id': 'one', 'version': 1, 'status': 'completed', 'generation': 0})
        session.user_speaking = True
        await session.announce_once()
        session.user_speaking = False
        session.playback_active = True
        await session.announce_once()
        assert session.provider.sent == []
        session.playback_active = False
        await session.announce_once()
        assert len([x for x in session.provider.sent if x['type'] == 'response.create']) == 1
    finally:
        await cleanup(session)


async def test_background_response_created_during_user_speech_is_cancelled():
    session = await session_with_broker(lambda r: httpx.Response(200, json={}))
    session.user_speaking = True
    session.provider = Wire([
        {'type': 'response.created', 'response': {'id': 'background', 'metadata': {'orbit_generation': '0'}}},
        {'type': 'response.output_audio.delta', 'response_id': 'background', 'item_id': 'audio', 'delta': 'AQI='},
    ])
    try:
        await session.receive_provider()
        assert b'\x01\x02' not in session.ws.sent
    finally:
        await cleanup(session)


async def test_reconnect_snapshot_removes_cancelled_reminder_commentary(monkeypatch):
    from orbit_voice import app
    class Events:
        def __init__(self, *args):
            pass
        async def frames(self):
            yield {'type': 'followup.snapshot', 'rules': [], 'due': []}
    monkeypatch.setattr(app, 'TaskEvents', Events)
    session = await session_with_broker(lambda r: httpx.Response(200, json={}))
    session.followup_pending['stale'] = {'delivery_id': 'stale', 'rule_id': 'cancelled'}
    try:
        await session.task_updates()
        assert session.followup_pending == {}
    finally:
        await cleanup(session)
