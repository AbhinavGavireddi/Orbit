from orbit_voice.protocol import OrderedTranscripts, conversation_config, transcription_config, control_phrase
from orbit_common.config import Settings
from orbit_voice.app import VoiceSession
import asyncio
import json
import httpx
import pytest


class Wire:
    def __init__(self, events=()):
        self.events, self.sent = list(events), []
    async def send_json(self, value):
        self.sent.append(value)
    async def send(self, value):
        self.sent.append(json.loads(value))
    async def send_bytes(self, value):
        self.sent.append(value)
    async def receive(self):
        if not self.events:
            return {"type": "websocket.disconnect"}
        return {"type": "websocket.receive", "text": json.dumps(self.events.pop(0))}
    async def close(self):
        pass
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return json.dumps(self.events.pop(0))


async def test_audio_continues_while_task_submission_waits_for_broker():
    requested, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def broker(request):
        calls.append(request.url.path)
        requested.set()
        await release.wait()
        return httpx.Response(200, json={"task_id": "background", "status": "queued"})
    native = Wire()
    session = VoiceSession(native, Settings(_env_file=None), "00000000-0000-0000-0000-000000000001", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.accept_turn("user-input")
    session.provider = Wire([
        {"type": "response.created", "response": {"id": "current", "metadata": {"orbit_generation": "0", "orbit_turn_id": "user-input"}}},
        {"type": "response.output_audio.delta", "response_id": "current", "item_id": "audio", "delta": "AQI="},
    ])
    tool = session.spawn(session.tool({"call_id": "slow-task", "name": "submit_task",
        "arguments": '{"goal":"Arrange the desktop","constraints":[],"completion_criteria":[]}'}, 0, "user-input"))
    try:
        await asyncio.wait_for(requested.wait(), 1)
        await session.receive_provider()
        assert b"\x01\x02" in native.sent
        assert not tool.done()
        release.set()
        await tool
        assert calls == ["/v1/tasks"]
        assert any(item.get("type") == "task.started" for item in native.sent if isinstance(item, dict))
    finally:
        release.set()
        await asyncio.gather(tool, return_exceptions=True)
        await session.http.aclose()


async def test_takeover_mode_exit_does_not_race_task_authority_or_flush_text():
    requests = []

    async def broker(request):
        requests.append(request)
        return httpx.Response(200, json={})

    native = Wire([{"type": "dictation.stop", "flush": False}])
    session = VoiceSession(native, Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    transcriber = Wire()
    session.transcriber = transcriber
    session.dictation = {"task_id": "taken-over", "note_id": "note"}
    session.pending_insertions.add("late-text")
    await session.receive_native()
    await asyncio.gather(*session.background)
    assert requests == []
    assert transcriber.sent == []
    assert session.dictation is None
    assert session.pending_insertions == set()
    assert native.sent == [{"type": "state", "state": "listening"}]
    await session.http.aclose()


async def test_old_response_cannot_restart_or_submit_after_stop_generation():
    native = Wire()
    session = VoiceSession(native, Settings(_env_file=None), "session", "mac")
    session.generation = 1
    session.paused_after_stop = True
    session.respond_pending = False
    provider = Wire([
        {"type": "response.created", "response": {"id": "old", "metadata": {"orbit_generation": "0"}}},
        {"type": "response.function_call_arguments.done", "response_id": "old", "call_id": "call-old",
         "name": "submit_task", "arguments": '{"kind":"automation","goal":"late"}'},
        {"type": "response.done", "response": {"id": "old"}},
    ])
    session.provider = provider
    await session.receive_provider()
    assert session.background == set()
    assert not any(item["type"] == "response.create" for item in provider.sent)
    assert "old" in session.cancelled_responses
    await session.http.aclose()


async def test_transcription_control_boundary_discards_later_completed_text():
    native = Wire()
    session = VoiceSession(native, Settings(_env_file=None), "session", "mac")
    session.dictation = {"task_id": "task", "note_id": "note"}
    session.transcriber = Wire([
        *({"type": "input_audio_buffer.committed", "item_id": str(n)} for n in [1, 2, 3]),
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "3", "transcript": "Must not insert"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "2", "transcript": "Orbit stop"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "1", "transcript": "First sentence"},
    ])
    await session.receive_transcription()
    assert [x["text"] for x in native.sent if x["type"] == "dictation.final"] == ["First sentence"]
    assert session.dictation_finishing is True
    for task in session.background:
        task.cancel()
    await asyncio.gather(*session.background, return_exceptions=True)
    await session.http.aclose()


def test_transcription_completion_order_does_not_reorder_dictated_sentences():
    buffer = OrderedTranscripts()
    buffer.commit("one")
    buffer.commit("two")
    assert buffer.complete("two", "Second sentence.") == []
    assert buffer.complete("one", "First sentence.") == [("one", "First sentence."), ("two", "Second sentence.")]
    assert buffer.complete("one", "First sentence.") == []


def test_reserved_control_phrase_is_not_inserted_into_notes():
    assert control_phrase("Orbit, finish dictation.") == "finish_dictation"
    assert control_phrase("Orbit, stop!") == "stop"
    assert control_phrase("I said goodbye to my friend.") is None
    assert control_phrase("orbiting satellites") is None


def test_conversation_and_dictation_use_separate_provider_modes():
    settings = Settings(_env_file=None)
    conversation = conversation_config(settings)["session"]
    transcription = transcription_config(settings)["session"]
    assert conversation["output_modalities"] == ["audio"]
    assert conversation["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert transcription["type"] == "transcription"
    assert "tools" not in transcription
    assert conversation["audio"]["input"]["transcription"]["language"] == "en"
    assert transcription["audio"]["input"]["transcription"]["language"] == "en"


async def test_final_transcript_refreshes_context_without_delaying_or_duplicating_speech():
    requested, release = asyncio.Event(), asyncio.Event()
    async def decision(request):
        requested.set()
        await release.wait()
        return httpx.Response(200, json={"available": False})
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(decision), base_url="http://broker")
    session.provider = Wire([
        {"type": "input_audio_buffer.speech_started", "item_id": "input-one"},
        {"type": "input_audio_buffer.committed", "item_id": "input-one"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "input-one", "transcript": "Open Notes"},
        {"type": "response.created", "response": {"id": "reply", "metadata": {"orbit_generation": "0", "orbit_turn_id": "input-one"}}},
        {"type": "response.output_audio.delta", "response_id": "reply", "item_id": "audio", "delta": "AQI="},
    ])
    try:
        await session.receive_provider()
        await asyncio.wait_for(requested.wait(), 1)
        assert b"\x01\x02" in session.ws.sent
        assert len([x for x in session.provider.sent if x["type"] == "response.create"]) == 1
        release.set()
        await asyncio.gather(*session.background)
        session.active_response = None
        await session.flush_pending_response()
        creation = next(x for x in session.provider.sent if x["type"] == "response.create")
        assert creation["response"]["metadata"]["orbit_turn_id"] == "input-one"
    finally:
        release.set()
        await session.http.aclose()


async def test_jev_shadow_and_unpromoted_capability_never_submit_tasks():
    for mode, capabilities in [("shadow", "open_notes"), ("active", "")]:
        submitted = []
        async def decision(request):
            if request.url.path == "/v1/tasks":
                submitted.append(request)
            return httpx.Response(200, json={"available": True, "capability": "open_notes", "direct": True,
                "confidence": .99, "probability": .99, "elapsed_ms": 80, "model": "jev-1.13.0"})
        config = Settings(_env_file=None, orbit_jev_mode=mode, orbit_jev_capabilities=capabilities)
        session = VoiceSession(Wire(), config, "session", "mac")
        await session.http.aclose()
        session.http = httpx.AsyncClient(transport=httpx.MockTransport(decision), base_url="http://broker")
        await session.route("Open Notes", "turn-1", 0)
        assert not submitted
        await session.http.aclose()


async def test_late_jev_after_stop_and_negated_requests_never_submit():
    for text, stop in [("Open Notes", True), ("Don't open Notes", False)]:
        submitted = []
        session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="active",
            orbit_jev_capabilities="open_notes"), "session", "mac")
        async def decision(request):
            if request.url.path == "/v1/tasks":
                submitted.append(request)
            if stop:
                session.generation += 1
            return httpx.Response(200, json={"available": True, "capability": "open_notes", "direct": True,
                "confidence": .99, "probability": .99, "elapsed_ms": 80, "model": "jev-1.13.0"})
        await session.http.aclose()
        session.http = httpx.AsyncClient(transport=httpx.MockTransport(decision), base_url="http://broker")
        await session.route(text, "turn-1", 0)
        assert submitted == []
        await session.http.aclose()


async def test_exact_open_notes_starts_dictation_when_jev_is_unsure():
    submitted = []

    async def broker(request):
        if request.url.path == "/v1/tasks":
            body = json.loads(request.content)
            submitted.append(body)
            return httpx.Response(200, json={**body, "task_id": "task", "version": 1, "status": "queued"})
        return httpx.Response(200, json={"available": True, "capability": "open_notes", "direct": True,
            "confidence": .69, "probability": .73, "elapsed_ms": 686, "model": "jev-1.13.0"})

    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="active",
        orbit_jev_capabilities="computer,start_dictation,open_notes"), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    await session.route("Open Notes", "turn-1", 0)
    await session.route("Open Calendar", "turn-2", 0)
    assert [body["capability"] for body in submitted] == ["start_dictation"]
    assert submitted[0]["kind"] == "dictation"
    await session.http.aclose()


async def test_eligible_jev_proposal_carries_turn_generation_and_fixed_capability():
    submitted = []
    async def broker(request):
        if request.url.path == "/v1/tasks":
            body = json.loads(request.content)
            submitted.append(body)
            return httpx.Response(200, json={**body, "task_id": "task", "version": 1, "status": "queued"})
        return httpx.Response(200, json={"available": True, "capability": "start_dictation", "direct": True,
            "confidence": .99, "probability": .99, "elapsed_ms": 80, "model": "jev-1.13.0"})
    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="active",
        orbit_jev_capabilities="start_dictation"), "session", "mac")
    session.provider = Wire()
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    await session.route("Orbit, please open Notes.", "turn-1", 0)
    await session.route("Orbit, please open Notes.", "turn-1", 0)
    assert len(submitted) == 1
    assert submitted[0]["turn_id"] == "turn-1"
    assert submitted[0]["generation"] == 0
    assert submitted[0]["capability"] == "start_dictation"
    assert submitted[0]["kind"] == "dictation"
    assert submitted[0]["route_source"] == "jev"
    assert submitted[0]["goal"] == "Start Notes dictation"
    await session.http.aclose()


async def test_jev_computer_decision_submits_the_users_words_for_any_desktop_request():
    submitted = []
    async def broker(request):
        if request.url.path == "/v1/tasks":
            body = json.loads(request.content)
            submitted.append(body)
            return httpx.Response(200, json={**body, "task_id": "task", "version": 1, "status": "queued",
                                              "route_source": "jev"})
        return httpx.Response(200, json={"available": True, "capability": "computer", "direct": True,
            "confidence": .99, "probability": .99, "elapsed_ms": 80, "model": "jev-1.13.0"})
    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="active",
        orbit_jev_capabilities="computer,start_dictation"), "session", "mac")
    session.provider = Wire()
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    await session.route("Open the browser and play Spotify", "turn-2", 0)
    assert len(submitted) == 1
    assert submitted[0]["capability"] == "computer"
    assert submitted[0]["kind"] == "automation"
    assert submitted[0]["goal"] == "Open the browser and play Spotify"
    assert submitted[0]["route_source"] == "jev"
    await session.http.aclose()


async def test_negated_or_quoted_computer_decision_does_not_run():
    for text in ["Don't open Calendar", 'He said "open Calendar"']:
        submitted = []
        async def broker(request):
            if request.url.path == "/v1/tasks":
                submitted.append(request)
            return httpx.Response(200, json={"available": True, "capability": "computer", "direct": True,
                "confidence": .99, "probability": .99, "elapsed_ms": 80, "model": "jev-1.13.0"})
        session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="active",
            orbit_jev_capabilities="computer"), "session", "mac")
        await session.http.aclose()
        session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
        await session.route(text, "turn-3", 0)
        assert submitted == []
        await session.http.aclose()


async def test_reconnected_snapshot_does_not_repeat_or_regress_task_notification():
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    session.provider = Wire()
    task = {"task_id": "done", "version": 3, "status": "completed", "generation": 0, "result": {"summary": "Ready"}}
    await session.task_update(task)
    await session.task_update({**task, "version": 1, "status": "queued"})
    await session.task_update(task)
    await session.announce_once()
    announcements = [x for x in session.provider.sent if x["type"] == "conversation.item.create"]
    assert len(announcements) == 1
    assert session.seen_tasks["done"] == 3
    await session.http.aclose()


@pytest.mark.parametrize("old_tool_first", [False, True])
async def test_new_user_turn_keeps_response_and_tool_owner_when_old_tool_finishes(old_tool_first):
    submitted = []

    async def broker(request):
        body = json.loads(request.content)
        submitted.append(body)
        return httpx.Response(200, json={**body, "task_id": body["request_id"], "status": "queued"})

    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="off"), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    try:
        await session.request_response("old-turn")
        session.provider.events = [{"type": "response.created", "response": {"id": "old-reply",
            "metadata": {"orbit_generation": "0", "orbit_turn_id": "old-turn"}}}]
        await session.receive_provider()
        old_tool = {"call_id": "old-task", "name": "submit_task",
                    "arguments": '{"kind":"research","goal":"Earlier topic"}'}
        if old_tool_first:
            await session.tool(old_tool, 0, "old-turn")
        await session.request_response("new-turn")
        if not old_tool_first:
            await session.tool(old_tool, 0, "old-turn")
        session.provider.events = [{"type": "response.done", "response": {"id": "old-reply"}}]
        await session.receive_provider()
        metadata = [x for x in session.provider.sent if x["type"] == "response.create"][-1]["response"]["metadata"]
        assert metadata == {"orbit_generation": "0", "orbit_turn_id": "new-turn", "orbit_input_epoch": "2"}
        session.provider.events = [
            {"type": "response.created", "response": {"id": "new-reply", "metadata": metadata}},
            {"type": "response.function_call_arguments.done", "response_id": "new-reply",
             "call_id": "new-task", "name": "submit_task",
             "arguments": '{"kind":"research","goal":"New topic"}'},
        ]
        await session.receive_provider()
        await asyncio.gather(*session.background)
        assert submitted == []
    finally:
        await session.http.aclose()


async def test_late_old_tool_cannot_restart_a_turn_after_new_turn_or_stop():
    async def broker(request):
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"generation": 1})
        return httpx.Response(200, json={"task_id": "old-task", "status": "queued"})

    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    try:
        await session.request_response("old-turn")
        session.provider.events = [
            {"type": "response.created", "response": {"id": "old-reply",
             "metadata": {"orbit_generation": "0", "orbit_turn_id": "old-turn"}}},
            {"type": "response.done", "response": {"id": "old-reply"}},
        ]
        await session.receive_provider()
        await session.request_response("new-turn")
        await session.tool({"call_id": "late-task", "name": "submit_task",
            "arguments": '{"kind":"research","goal":"Earlier topic"}'}, 0, "old-turn")
        session.provider.events = [
            {"type": "response.created", "response": {"id": "new-reply",
             "metadata": {"orbit_generation": "0", "orbit_turn_id": "new-turn"}}},
            {"type": "response.done", "response": {"id": "new-reply"}},
        ]
        await session.receive_provider()
        assert len([x for x in session.provider.sent if x["type"] == "response.create"]) == 2
        await session.control("stop")
        session.paused_after_stop = False
        await session.request_response("fresh-turn")
        await session.tool({"call_id": "stale-task", "name": "submit_task",
            "arguments": '{"kind":"research","goal":"Stale topic"}'}, 0, "old-turn")
        assert [x for x in session.provider.sent if x["type"] == "response.create"][-1]["response"]["metadata"] == {
            "orbit_generation": "1", "orbit_turn_id": "fresh-turn", "orbit_input_epoch": str(session.input_epoch)}
    finally:
        await session.http.aclose()


async def test_background_announcement_does_not_grant_an_executable_turn():
    submitted = []
    async def broker(request):
        submitted.append(request)
        return httpx.Response(200, json={"task_id": "unexpected"})
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    try:
        await session.task_update({"task_id": "done", "version": 3, "status": "completed", "generation": 0})
        await session.announce_once()
        metadata = session.provider.sent[-1]["response"]["metadata"]
        assert "orbit_turn_id" not in metadata
        session.provider.events = [
            {"type": "response.created", "response": {"id": "announcement", "metadata": metadata}},
            {"type": "response.function_call_arguments.done", "response_id": "announcement",
             "call_id": "invented-task", "name": "submit_task",
             "arguments": '{"kind":"research","goal":"Unrequested work"}'},
        ]
        await session.receive_provider()
        await asyncio.gather(*session.background)
        assert submitted == []
    finally:
        await session.http.aclose()


async def test_server_interrupt_waits_off_receive_loop_for_correlated_final_heard_position():
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    session.provider = Wire()
    session.active_response = "reply"
    session.last_audio_item = "audio"
    session.played = {"item_id": "audio", "content_index": 0, "audio_end_ms": 900}
    try:
        await asyncio.wait_for(session.interrupt(), 0.1)
        assert not [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"]
        clear = session.ws.sent[-1]
        assert clear["type"] == "audio.clear"
        assert clear["generation"] == 0
        assert [x["type"] for x in session.provider.sent] == ["response.cancel"]
        ack = {"type": "audio.cleared", "interruption_id": clear["interruption_id"], "generation": 0,
               "positions": [{"item_id": "audio", "content_index": 0, "audio_end_ms": 990}]}
        session.ws.events = [{**ack, "generation": 1}, {**ack, "interruption_id": "unrelated"}, ack, ack]
        await asyncio.wait_for(session.receive_native(), 0.1)
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"] == [
            {"type": "conversation.item.truncate", "item_id": "audio", "content_index": 0, "audio_end_ms": 990}]
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


@pytest.mark.parametrize("played", [None, 900])
async def test_legacy_interrupt_has_bounded_fallback_and_repeated_clear_never_truncates_twice(played):
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    session.provider = Wire()
    session.last_audio_item = "audio"
    if played is not None:
        session.played = {"item_id": "audio", "content_index": 0, "audio_end_ms": played}
    try:
        await session.interrupt()
        clear = session.ws.sent[-1]
        await session.interrupt()
        await asyncio.wait_for(asyncio.gather(*session.background), 0.6)
        truncates = [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"]
        assert truncates == [{"type": "conversation.item.truncate", "item_id": "audio",
                              "content_index": 0, "audio_end_ms": played or 0}]
        session.ws.events = [{"type": "audio.cleared", "interruption_id": clear["interruption_id"],
                              "generation": 0, "positions": [{"item_id": "audio", "content_index": 0, "audio_end_ms": 1000}]}]
        await session.receive_native()
        assert [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"] == truncates
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


@pytest.mark.parametrize("command", ["interrupt", "stop", "goodbye"])
async def test_local_interrupt_controls_use_direct_final_position_without_waiting_for_ack(command):
    async def broker(request):
        return httpx.Response(200, json={"generation": 1})
    frame = {"type": "interrupt"} if command == "interrupt" else {"type": "control", "command": command}
    frame["positions"] = [{"item_id": "audio", "content_index": 0, "audio_end_ms": 990}]
    session = VoiceSession(Wire([frame]), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    session.active_response = "reply"
    session.last_audio_item = "audio"
    session.played = {"item_id": "audio", "content_index": 0, "audio_end_ms": 900}
    try:
        await asyncio.wait_for(session.receive_native(), 0.1)
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"] == [
            {"type": "conversation.item.truncate", "item_id": "audio", "content_index": 0, "audio_end_ms": 990}]
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_stop_broker_wait_does_not_block_native_clear_acknowledgement():
    class QueuedNative(Wire):
        def __init__(self):
            super().__init__()
            self.incoming = asyncio.Queue()
        async def receive(self):
            frame = await self.incoming.get()
            return {"type": "websocket.receive", "text": json.dumps(frame)} if frame else {"type": "websocket.disconnect"}

    started, release = asyncio.Event(), asyncio.Event()
    async def broker(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"generation": 1})
    native = QueuedNative()
    session = VoiceSession(native, Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    session.last_audio_item = "audio"
    session.played = {"item_id": "audio", "content_index": 0, "audio_end_ms": 900}
    receiver = session.spawn(session.receive_native())
    try:
        await native.incoming.put({"type": "control", "command": "stop"})
        await asyncio.wait_for(started.wait(), 0.1)
        clear = native.sent[-1]
        await native.incoming.put({"type": "audio.cleared", "interruption_id": clear["interruption_id"],
            "generation": 0, "positions": [{"item_id": "audio", "content_index": 0, "audio_end_ms": 990}]})
        async with asyncio.timeout(0.1):
            while not any(x["type"] == "conversation.item.truncate" for x in session.provider.sent):
                await asyncio.sleep(0.001)
        assert session.generation == 0
        assert session.provider.sent[-1]["audio_end_ms"] == 990
        release.set()
        async with asyncio.timeout(0.1):
            while session.generation != 1:
                await asyncio.sleep(0.001)
        await native.incoming.put(None)
        await receiver
        await asyncio.gather(*session.background)
    finally:
        release.set()
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_provider_clear_keeps_reading_and_old_ack_cannot_truncate_new_item():
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    session.active_response = "old-response"
    session.last_audio_item = "old-audio"
    session.played = {"item_id": "old-audio", "content_index": 0, "audio_end_ms": 900}
    session.provider = Wire([
        {"type": "input_audio_buffer.speech_started", "item_id": "new-turn"},
        {"type": "response.done", "response": {"id": "old-response"}},
        {"type": "input_audio_buffer.committed", "item_id": "new-turn"},
        {"type": "response.created", "response": {"id": "new-response",
         "metadata": {"orbit_generation": "0", "orbit_turn_id": "new-turn"}}},
        {"type": "response.output_audio.delta", "response_id": "new-response", "item_id": "new-audio",
         "content_index": 2, "delta": "AQI="},
    ])
    try:
        await asyncio.wait_for(session.receive_provider(), 0.1)
        assert b"\x01\x02" in session.ws.sent
        clear = next(x for x in session.ws.sent if isinstance(x, dict) and x["type"] == "audio.clear")
        session.generation = 1
        session.ws.events = [{"type": "audio.cleared", "interruption_id": clear["interruption_id"],
            "generation": 0, "positions": [{"item_id": "old-audio", "content_index": 0, "audio_end_ms": 990}]}]
        await session.receive_native()
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert session.last_audio_item == "new-audio"
        await session.interrupt(positions=[{"item_id": "new-audio", "content_index": 2, "audio_end_ms": 0}])
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"] == [
            {"type": "conversation.item.truncate", "item_id": "old-audio", "content_index": 0, "audio_end_ms": 990},
            {"type": "conversation.item.truncate", "item_id": "new-audio", "content_index": 2, "audio_end_ms": 0},
        ]
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_dictation_start_preserves_local_final_heard_position(monkeypatch):
    async def broker(request):
        return httpx.Response(200, json={"session_id": "session", "kind": "dictation", "status": "running",
                                        "result": {"note_id": "note"}})
    transcriber = Wire()
    async def connect(*args, **kwargs):
        return transcriber
    monkeypatch.setattr("orbit_voice.app.connect", connect)
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    session.last_audio_item = "audio"
    session.played = {"item_id": "audio", "content_index": 0, "audio_end_ms": 900}
    try:
        await session.start_dictation({"task_id": "task", "note_id": "note", "positions": [
            {"item_id": "audio", "content_index": 0, "audio_end_ms": 990}]})
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert session.dictation == {"task_id": "task", "note_id": "note"}
        assert [x for x in session.provider.sent if x["type"] == "conversation.item.truncate"] == [
            {"type": "conversation.item.truncate", "item_id": "audio", "content_index": 0, "audio_end_ms": 990}]
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


@pytest.mark.parametrize("stage", ["broker", "connection"])
async def test_stop_during_background_dictation_start_cannot_open_a_stale_stream(monkeypatch, stage):
    started, release = asyncio.Event(), asyncio.Event()
    connected = []
    class Transcriber(Wire):
        closed = False
        async def close(self):
            self.closed = True
    transcriber = Transcriber()
    async def connect(*args, **kwargs):
        connected.append(True)
        if stage == "connection":
            started.set()
            await release.wait()
        return transcriber
    monkeypatch.setattr("orbit_voice.app.connect", connect)
    async def broker(request):
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"generation": 1})
        if stage == "broker":
            started.set()
            await release.wait()
        return httpx.Response(200, json={"session_id": "session", "kind": "dictation", "status": "running",
                                        "result": {"note_id": "note"}})
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    starting = session.spawn(session.start_dictation({"task_id": "task", "note_id": "note", "positions": []}))
    try:
        await asyncio.wait_for(started.wait(), 0.1)
        await session.control("stop")
        release.set()
        await asyncio.wait_for(starting, 0.1)
        await asyncio.gather(*session.background)
        assert session.dictation is None
        assert session.transcriber is None
        assert transcriber.sent == []
        assert connected == ([True] if stage == "connection" else [])
        if stage == "connection":
            assert transcriber.closed
    finally:
        release.set()
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


@pytest.mark.parametrize("acknowledge", [False, True])
async def test_new_response_waits_for_final_truncate_send_and_keeps_latest_turn(acknowledge):
    truncating, release = asyncio.Event(), asyncio.Event()
    class SlowTruncate(Wire):
        async def send(self, value):
            if json.loads(value)["type"] == "conversation.item.truncate":
                truncating.set()
                await release.wait()
            await super().send(value)

    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="off"), "session", "mac")
    session.active_response, session.last_audio_item = "old-response", "old-audio"
    session.played = {"item_id": "old-audio", "content_index": 0, "audio_end_ms": 900}
    session.provider = SlowTruncate([
        {"type": "input_audio_buffer.speech_started", "item_id": "new-turn"},
        {"type": "response.done", "response": {"id": "old-response"}},
        {"type": "input_audio_buffer.committed", "item_id": "new-turn"},
    ])
    try:
        await asyncio.wait_for(session.receive_provider(), 0.1)
        assert [x["type"] for x in session.provider.sent] == ["response.cancel"]
        clear = next(x for x in session.ws.sent if x["type"] == "audio.clear")
        if acknowledge:
            ack = {"type": "audio.cleared", "interruption_id": clear["interruption_id"], "generation": 0,
                   "positions": [{"item_id": "old-audio", "content_index": 0, "audio_end_ms": 990}]}
            session.ws.events = [ack, ack]
            await asyncio.wait_for(session.receive_native(), 0.1)
        await asyncio.wait_for(truncating.wait(), 0.6)
        # Even after acknowledgement, the actual truncate write is still held.
        # Neither loop may wait on it, and only the latest accepted turn wins.
        session.ws.events = [{"type": "text", "text": "A newer question"}]
        await asyncio.wait_for(session.receive_native(), 0.1)
        newest = session.latest_turn
        await session.request_response("new-turn", continuation=True)
        await session.request_response()  # A background announcement cannot replace its owner.
        assert not [x for x in session.provider.sent if x["type"] == "response.create"]
        release.set()
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        response_order = [x["type"] for x in session.provider.sent if x["type"] != "conversation.item.create"]
        assert response_order == ["response.cancel", "conversation.item.truncate", "response.create"]
        truncate = next(x for x in session.provider.sent if x["type"] == "conversation.item.truncate")
        assert truncate["audio_end_ms"] == (990 if acknowledge else 900)
        creation = next(x for x in session.provider.sent if x["type"] == "response.create")
        assert creation["response"]["metadata"] == {"orbit_generation": "0", "orbit_turn_id": newest, "orbit_input_epoch": str(session.input_epoch)}
        assert session.respond_pending is False
    finally:
        release.set()
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


@pytest.mark.parametrize("command", ["stop", "goodbye"])
async def test_control_while_truncation_is_pending_discards_queued_old_response(command):
    async def broker(request):
        return httpx.Response(200, json={"generation": 1})
    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="off"), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.active_response, session.last_audio_item = "old-response", "old-audio"
    session.provider = Wire([
        {"type": "input_audio_buffer.speech_started", "item_id": "new-turn"},
        {"type": "response.done", "response": {"id": "old-response"}},
        {"type": "input_audio_buffer.committed", "item_id": "new-turn"},
    ])
    try:
        await session.receive_provider()
        assert not [x for x in session.provider.sent if x["type"] == "response.create"]
        clear = next(x for x in session.ws.sent if x["type"] == "audio.clear")
        control = session.spawn(session.control(command))
        await asyncio.sleep(0)
        assert session.generation == 1
        session.ws.events = [{"type": "audio.cleared", "interruption_id": clear["interruption_id"],
            "generation": 0, "positions": [{"item_id": "old-audio", "content_index": 0, "audio_end_ms": 990}]}]
        await asyncio.wait_for(session.receive_native(), 0.1)
        await asyncio.wait_for(control, 0.1)
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert not [x for x in session.provider.sent if x["type"] == "response.create"]
        assert session.respond_pending is False
        assert session.closed is (command == "goodbye")
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_old_generation_truncate_releases_only_fresh_turn_after_stop():
    async def broker(request):
        return httpx.Response(200, json={"generation": 1})
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    session.last_audio_item = "old-audio"
    try:
        await session.interrupt()
        clear = session.ws.sent[-1]
        await session.request_response("stale-turn")
        await session.control("stop")
        session.paused_after_stop = False
        await session.request_response("fresh-turn")
        assert not [x for x in session.provider.sent if x["type"] == "response.create"]
        session.ws.events = [{"type": "audio.cleared", "interruption_id": clear["interruption_id"], "generation": 0,
            "positions": [{"item_id": "old-audio", "content_index": 0, "audio_end_ms": 990}]}]
        await session.receive_native()
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        creations = [x for x in session.provider.sent if x["type"] == "response.create"]
        assert len(creations) == 1
        assert creations[0]["response"]["metadata"] == {"orbit_generation": "1", "orbit_turn_id": "fresh-turn", "orbit_input_epoch": str(session.input_epoch)}
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_overlapping_interruptions_release_one_response_after_both_truncates():
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    session.provider = Wire()
    try:
        for item in ["first-audio", "second-audio"]:
            session.last_audio_item = item
            await session.interrupt()
        await session.request_response("new-turn")
        clears = [x for x in session.ws.sent if x["type"] == "audio.clear"]
        for index in [1, 0]:
            item = ["first-audio", "second-audio"][index]
            ack = {"type": "audio.cleared", "interruption_id": clears[index]["interruption_id"], "generation": 0,
                   "positions": [{"item_id": item, "content_index": 0, "audio_end_ms": 0}]}
            session.ws.events = [ack, ack]
            await session.receive_native()
            async with asyncio.timeout(0.1):
                while not any(x.get("item_id") == item for x in session.provider.sent):
                    await asyncio.sleep(0.001)
            if index == 1:
                assert not [x for x in session.provider.sent if x["type"] == "response.create"]
        await asyncio.wait_for(asyncio.gather(*session.background), 0.1)
        assert [x["type"] for x in session.provider.sent] == [
            "conversation.item.truncate", "conversation.item.truncate", "response.create"]
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


@pytest.mark.parametrize("failure", ["disconnect", "stall"])
async def test_failed_or_stalled_truncate_never_releases_a_response_against_untrimmed_context(monkeypatch, failure):
    monkeypatch.setattr("orbit_voice.app.AUDIO_TRUNCATE_SEND_TIMEOUT", 0.02, raising=False)
    class FailedTruncate(Wire):
        async def send(self, value):
            if json.loads(value)["type"] == "conversation.item.truncate":
                if failure == "disconnect":
                    raise ConnectionError("Provider disconnected before truncate")
                await asyncio.Event().wait()
            await super().send(value)
    session = VoiceSession(Wire(), Settings(_env_file=None), "session", "mac")
    session.provider = FailedTruncate()
    session.last_audio_item = "old-audio"
    try:
        await session.interrupt(positions=[])
        await session.request_response("next-turn")
        done, pending = await asyncio.wait(list(session.background), timeout=0.1)
        await asyncio.gather(*done, return_exceptions=True)
        assert not pending, "A blocked truncate write must have a bounded failure path"
        assert session.closed
        await session.request_response("another-turn")
        assert not [x for x in session.provider.sent if x["type"] == "response.create"]
        assert any(x["type"] == "error" for x in session.ws.sent)
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_old_reply_audio_stays_silent_after_a_new_sentence():
    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="off"), "session", "face")
    session.accept_turn("new-turn")
    session.active_response = "old-response"
    session.audible_response = "old-response"
    session.response_generations["old-response"] = 0
    session.last_audio_item = "old-audio"
    session.played = {"item_id": "old-audio", "content_index": 0, "audio_end_ms": 100}
    session.provider = Wire()
    try:
        await session.interrupt()
        session.provider.events = [
            {"type": "response.output_audio.delta", "response_id": "old-response",
             "item_id": "old-audio", "delta": "AQI="},
            {"type": "response.created", "response": {"id": "new-response",
             "metadata": {"orbit_generation": "0", "orbit_turn_id": "new-turn"}}},
            {"type": "response.output_audio.delta", "response_id": "new-response",
             "item_id": "new-audio", "delta": "AgM="},
        ]
        await session.receive_provider()
        audio = [item for item in session.ws.sent if isinstance(item, bytes)]
        assert audio == [b"\x02\x03"]
        assert session.audible_response == "new-response"
        clear = next(item for item in session.ws.sent if isinstance(item, dict) and item.get("type") == "audio.clear")
        session.ws.events = [{"type": "audio.cleared", "interruption_id": clear["interruption_id"],
            "generation": 0, "positions": [{"item_id": "old-audio", "content_index": 0, "audio_end_ms": 100}]}]
        await session.receive_native()
        await asyncio.wait_for(asyncio.gather(*list(session.background)), 1)
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_a_turn_tells_the_model_the_current_time():
    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="off"), "session", "face")
    session.provider = Wire()
    session.accept_turn("one")
    try:
        await session.request_response("one")
        clock = next(item for item in session.provider.sent
                     if item.get("type") == "conversation.item.create"
                     and "Current UTC time:" in item["item"]["content"][0]["text"])
        assert "follow_up" in clock["item"]["content"][0]["text"]
    finally:
        await session.http.aclose()


async def test_a_reminder_goal_does_not_start_a_task():
    calls = []

    async def broker(request):
        calls.append(request.url.path)
        return httpx.Response(202, json={"task_id": "task", "status": "queued", "version": 1})

    session = VoiceSession(Wire(), Settings(_env_file=None, orbit_jev_mode="off"), "session", "face")
    await session.http.aclose()
    session.http = httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="http://broker")
    session.provider = Wire()
    session.accept_turn("one")
    try:
        await session.tool({"call_id": "remind", "name": "submit_task", "arguments": json.dumps({
            "goal": "Create a reminder for the user in Asia/Kolkata to stretch in one minute.",
            "constraints": [], "completion_criteria": []})}, 0, "one")
        assert calls == []
        output = next(item["item"]["output"] for item in session.provider.sent
                      if item.get("type") == "conversation.item.create"
                      and item["item"]["type"] == "function_call_output")
        assert "follow_up" in output
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


async def test_typed_text_cancels_the_answer_still_playing():
    session = VoiceSession(Wire([{"type": "text", "text": "Next question", "positions": [
        {"item_id": "old-audio", "content_index": 0, "audio_end_ms": 450}] }]),
        Settings(_env_file=None, orbit_jev_mode="off"), "session", "face")
    session.active_response = "old-response"
    session.last_audio_item = "old-audio"
    session.played = {"item_id": "old-audio", "content_index": 0, "audio_end_ms": 400}
    session.provider = Wire()
    try:
        await asyncio.wait_for(session.receive_native(), 0.2)
        sent = [item["type"] for item in session.provider.sent]
        assert sent[0] == "response.cancel"
        assert "conversation.item.create" in sent
        assert any(item["type"] == "audio.clear" for item in session.ws.sent)
        assert "old-response" in session.cancelled_responses
        await asyncio.wait_for(asyncio.gather(*list(session.background)), 1)
    finally:
        for task in list(session.background):
            task.cancel()
        await asyncio.gather(*session.background, return_exceptions=True)
        await session.http.aclose()


def _local_settings():
    return Settings(_env_file=None, openai_api_key="", orbit_device_token="d" * 40,
                    orbit_service_token="s" * 40)


async def test_face_ticket_reaches_the_same_voice_gate():
    import fakeredis.aioredis
    from fastapi.testclient import TestClient
    from orbit_voice.app import create_app, ticket_ok
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set("orbit:face-ticket:abc", "face\nsid")
    assert await ticket_ok(redis, "used", "face", "sid") is False
    await redis.set("orbit:face-ticket:abc", "face\nsid")
    with TestClient(create_app(_local_settings(), redis)) as client:
        with client.websocket_connect("/v1/voice?session_id=sid&device_id=face&ticket=abc") as ws:
            frame = ws.receive_json()
            assert frame["type"] == "error"
            assert "OPENAI_API_KEY" in frame["message"]
        with pytest.raises(Exception):
            with client.websocket_connect("/v1/voice?session_id=sid&device_id=face&ticket=abc"):
                pass
    await redis.aclose()
