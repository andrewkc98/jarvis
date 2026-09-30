import asyncio
import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest

from jarvis.orchestrator.service import JarvisService, _speak_stream
from jarvis.orchestrator import router, sdk_backend
from jarvis.providers import schedule_provider
from jarvis.runtime import status as runtime_status
from jarvis.telemetry import store as telemetry_store
from jarvis.voice import capture, stt, tts


async def _fake_ask_stream(chunks):
    for chunk in chunks:
        yield chunk


def _stub_runtime_status(monkeypatch):
    calls = []
    owner = object()
    monkeypatch.setattr(runtime_status, "publisher", lambda source: owner)
    monkeypatch.setattr(
        runtime_status,
        "publish",
        lambda actual_owner, state, turn_id: calls.append(
            ("publish", actual_owner, state, turn_id)
        ),
    )
    monkeypatch.setattr(
        runtime_status,
        "clear",
        lambda actual_owner, turn_id: calls.append(
            ("clear", actual_owner, turn_id)
        ),
    )
    monkeypatch.setattr(
        runtime_status,
        "fail",
        lambda actual_owner, turn_id, code: calls.append(
            ("fail", actual_owner, turn_id, code)
        ),
    )
    return calls, owner


class _RecordingSpeaker:
    def __init__(self):
        self.calls = []

    def say(self, text):
        self.calls.append(text)


def test_speak_stream_speaks_first_sentence_before_producer_yields_the_next_chunk():
    release = asyncio.Event()
    order = []

    async def chunks():
        yield "First sentence. "
        await release.wait()
        order.append("yielded-second")
        yield "Second sentence."

    speaker = _RecordingSpeaker()
    original_say = speaker.say

    def say_and_release(text):
        original_say(text)
        order.append(("spoke", text))
        release.set()

    speaker.say = say_and_release

    result = asyncio.run(_speak_stream(chunks(), speaker))

    assert order.index(("spoke", "First sentence.")) < order.index("yielded-second")
    assert speaker.calls == ["First sentence.", "Second sentence."]
    assert result == "First sentence. Second sentence."


def test_speak_stream_propagates_producer_error_after_draining_the_queue():
    async def chunks():
        yield "Partial "
        raise RuntimeError("stream broke")

    speaker = _RecordingSpeaker()
    try:
        asyncio.run(_speak_stream(chunks(), speaker))
    except RuntimeError as error:
        assert "stream broke" in str(error)
    else:
        raise AssertionError("expected RuntimeError")
    assert speaker.calls == ["Partial"]


def test_speak_stream_flushes_trailing_text_without_terminal_punctuation():
    async def chunks():
        yield "No terminator here"

    speaker = _RecordingSpeaker()
    result = asyncio.run(_speak_stream(chunks(), speaker))
    assert speaker.calls == ["No terminator here"]
    assert result == "No terminator here"


def test_speak_stream_returns_full_text_even_with_multiple_sentences():
    async def chunks():
        yield "One. "
        yield "Two. Three."

    speaker = _RecordingSpeaker()
    result = asyncio.run(_speak_stream(chunks(), speaker))
    assert speaker.calls == ["One.", "Two.", "Three."]
    assert result == "One. Two. Three."


def test_speak_stream_uses_one_dedicated_thread_for_every_say_call():
    calling_thread_ids = []

    class _ThreadRecordingSpeaker:
        def say(self, text):
            calling_thread_ids.append(threading.get_ident())

    async def chunks():
        yield "One. "
        yield "Two. Three."

    asyncio.run(_speak_stream(chunks(), _ThreadRecordingSpeaker()))

    assert len(calling_thread_ids) == 3
    assert len(set(calling_thread_ids)) == 1
    assert calling_thread_ids[0] != threading.get_ident()


def test_run_text_skips_capture_and_transcription(monkeypatch):
    capture_mock = Mock()
    stt_mock = Mock()
    speak_mock = Mock()
    monkeypatch.setattr(capture, "record_on_enter", capture_mock)
    monkeypatch.setattr(stt, "transcribe", stt_mock)
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(
        sdk_backend.SDKBackend, "ask_stream",
        lambda self, prompt: _fake_ask_stream(["vault answer"]),
    )

    service = JarvisService(voice_model_path="voice.onnx")
    result = asyncio.run(service.arun_text("hello world"))

    assert result == "vault answer"
    capture_mock.assert_not_called()
    stt_mock.assert_not_called()
    speak_mock.assert_called_once_with("vault answer")


def test_run_once_calls_capture_stt_route_and_ask_stream_in_order(monkeypatch):
    audio = np.array([0.1, 0.2], dtype=np.float32)
    events = []
    monkeypatch.setattr(
        capture, "record_on_enter",
        Mock(
            side_effect=lambda **kwargs: (events.append("capture"), audio)[1]
        ),
    )
    monkeypatch.setattr(
        stt, "transcribe",
        Mock(side_effect=lambda value: (events.append(("stt", value)), "testing one two three")[1]),
    )
    monkeypatch.setattr(
        router, "route",
        lambda text: (events.append(("route", text)), "fallback")[1],
    )

    def fake_ask_stream(self, prompt):
        events.append(("ask_stream", prompt))
        return _fake_ask_stream(["spoken answer"])

    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", fake_ask_stream)
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: events.append(("tts", text)))

    service = JarvisService(voice_model_path="voice.onnx")
    result = asyncio.run(service.arun_once())

    assert result == "spoken answer"
    assert events == [
        "capture",
        ("stt", audio),
        ("route", "testing one two three"),
        ("ask_stream", "testing one two three"),
        ("tts", "spoken answer"),
    ]


def test_voice_status_order_uses_one_turn_id(monkeypatch):
    calls, owner = _stub_runtime_status(monkeypatch)
    audio = np.array([0.1], dtype=np.float32)

    def capture_start(**kwargs):
        kwargs["on_started"]()
        return audio

    monkeypatch.setattr(capture, "record_on_enter", capture_start)
    monkeypatch.setattr(stt, "transcribe", lambda value: "hello")
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(
        sdk_backend.SDKBackend,
        "ask_stream",
        lambda self, prompt: _fake_ask_stream(["answer"]),
    )
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)

    asyncio.run(JarvisService("voice.onnx", runtime_source="cli").arun_once())

    assert [call[0] for call in calls] == [
        "publish",
        "publish",
        "publish",
        "clear",
    ]
    turn_ids = [call[-1] for call in calls]
    assert len(set(turn_ids)) == 1
    assert all(call[1] is owner for call in calls)
    assert [call[2] for call in calls[:3]] == ["listening", "processing", "speaking"]


@pytest.mark.parametrize("speak", [True, False])
@pytest.mark.parametrize("error", [PermissionError("denied"), RuntimeError("down")])
def test_schedule_handled_error_persists_after_optional_reply(
    monkeypatch, speak, error
):
    calls, _ = _stub_runtime_status(monkeypatch)
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    monkeypatch.setattr(
        schedule_provider,
        "get_upcoming_events",
        Mock(side_effect=error),
    )
    spoken = []
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: spoken.append(text))

    result = asyncio.run(
        JarvisService("voice.onnx").aanswer("calendar", speak=speak)
    )

    assert result
    assert bool(spoken) is speak
    fail_calls = [call for call in calls if call[0] == "fail"]
    assert len(fail_calls) == 1
    assert fail_calls[0][-1] == "schedule_failed"
    fail_index = calls.index(fail_calls[0])
    assert not any(call[0] == "clear" for call in calls[:fail_index])


def test_cancelled_stream_failure_is_recorded_without_orphan_task(monkeypatch):
    calls, _ = _stub_runtime_status(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def chunks():
        started.set()
        await release.wait()
        raise ConnectionError("late stream failure")
        yield  # pragma: no cover

    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(
        sdk_backend.SDKBackend, "ask_stream", lambda self, prompt: chunks()
    )
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)

    async def run():
        service = JarvisService("voice.onnx")
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: loop_errors.append(context))
        turn = asyncio.create_task(service.aanswer("hello"))
        try:
            await started.wait()
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
            release.set()
            await asyncio.sleep(0.05)
            assert any(
                call[0] == "fail" and call[-1] == "sdk_failed" for call in calls
            )
            assert service._active_turn_id is None
            assert loop_errors == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(run())


def test_late_playback_cleanup_does_not_clear_newer_turn(monkeypatch):
    calls = []
    owner = object()
    active = []
    monkeypatch.setattr(runtime_status, "publisher", lambda source: owner)
    monkeypatch.setattr(
        runtime_status,
        "publish",
        lambda actual_owner, state, turn_id: (active.clear(), active.append(turn_id), calls.append(("publish", state, turn_id))),
    )
    def compare_clear(actual_owner, turn_id):
        calls.append(("clear", turn_id))
        if active == [turn_id]:
            active.clear()
    monkeypatch.setattr(runtime_status, "clear", compare_clear)
    monkeypatch.setattr(
        runtime_status,
        "fail",
        lambda actual_owner, turn_id, code: calls.append(("fail", turn_id, code)),
    )
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def chunks(prompt):
        if prompt == "first":
            yield "first"
        else:
            second_started.set()
            await release_second.wait()
            yield "second"

    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(
        sdk_backend.SDKBackend,
        "ask_stream",
        lambda self, prompt: chunks(prompt),
    )

    def say(text):
        first_started.set()
        release_first.wait()

    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: say(text))

    async def run():
        service = JarvisService("voice.onnx")
        first = asyncio.create_task(service.aanswer("first"))
        await asyncio.to_thread(first_started.wait)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(service.aanswer("second", speak=False))
        await second_started.wait()
        release_first.set()
        await asyncio.sleep(0.05)

        assert active and len(active) == 1

        release_second.set()
        assert await second == "second"

    asyncio.run(run())


def test_close_clears_active_non_playback_turn_after_backend_close(monkeypatch):
    calls, _ = _stub_runtime_status(monkeypatch)
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def chunks():
        stream_started.set()
        await release_stream.wait()
        yield "answer"

    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(
        sdk_backend.SDKBackend, "ask_stream", lambda self, prompt: chunks()
    )

    async def fake_close(self):
        close_started.set()
        await release_close.wait()

    monkeypatch.setattr(sdk_backend.SDKBackend, "close", fake_close)

    async def run():
        service = JarvisService("voice.onnx")
        turn = asyncio.create_task(service.aanswer("hello", speak=False))
        await stream_started.wait()
        closing = asyncio.create_task(service.aclose())
        await close_started.wait()
        await asyncio.sleep(0)
        assert not any(call[0] == "clear" for call in calls)
        release_close.set()
        await closing
        assert any(call[0] == "clear" for call in calls)
        release_stream.set()
        assert await turn == "answer"

    asyncio.run(run())


def test_schedule_query_uses_schedule_provider_and_never_touches_sdk_backend(monkeypatch):
    events = [{"title": "Team standup"}, {"summary": "Dentist"}]
    schedule_mock = Mock(return_value=events)
    speak_mock = Mock()
    monkeypatch.setattr(schedule_provider, "get_upcoming_events", schedule_mock)
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))

    def poison_ask_stream(self, prompt):
        raise AssertionError("schedule-routed turn must not call SDKBackend.ask_stream")

    def poison_connect(self):
        raise AssertionError("schedule-routed turn must not connect the SDK client")

    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", poison_ask_stream)
    monkeypatch.setattr(sdk_backend.SDKBackend, "_ensure_connected", poison_connect)

    result = asyncio.run(JarvisService("voice.onnx").arun_text("What is on my calendar?"))

    assert result == "You have 2 events: Team standup, Dentist"
    schedule_mock.assert_called_once_with()
    speak_mock.assert_called_once_with(result)


def test_non_schedule_query_uses_sdk_backend(monkeypatch):
    schedule_mock = Mock()
    speak_mock = Mock()
    monkeypatch.setattr(schedule_provider, "get_upcoming_events", schedule_mock)
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))
    monkeypatch.setattr(
        sdk_backend.SDKBackend, "ask_stream",
        lambda self, prompt: _fake_ask_stream(["The vault says hello."]),
    )

    result = asyncio.run(
        JarvisService("voice.onnx").arun_text("What did I write about Jarvis?")
    )

    assert result == "The vault says hello."
    schedule_mock.assert_not_called()
    speak_mock.assert_called_once_with(result)


def test_schedule_permission_error_is_spoken_not_raised(monkeypatch):
    monkeypatch.setattr(
        schedule_provider, "get_upcoming_events",
        Mock(side_effect=PermissionError("denied")),
    )
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    speak_mock = Mock()
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))

    result = asyncio.run(JarvisService("voice.onnx").arun_text("what's on my calendar"))

    assert "authorized" in result.lower()
    speak_mock.assert_called_once_with(result)


def test_schedule_runtime_error_is_spoken_not_raised(monkeypatch):
    monkeypatch.setattr(
        schedule_provider, "get_upcoming_events",
        Mock(side_effect=RuntimeError("timed out")),
    )
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    speak_mock = Mock()
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))

    result = asyncio.run(JarvisService("voice.onnx").arun_text("what's on my calendar"))

    assert "timed out" in result
    speak_mock.assert_called_once_with(result)


def test_aclose_closes_sdk_backend(monkeypatch):
    close_mock = Mock()

    async def fake_close(self):
        close_mock()

    monkeypatch.setattr(sdk_backend.SDKBackend, "close", fake_close)

    service = JarvisService("voice.onnx")
    asyncio.run(service.aclose())
    close_mock.assert_called_once_with()


def _entry(telemetry_mock):
    assert telemetry_mock.append_entry.call_count == 1
    return telemetry_mock.append_entry.call_args.args[0]


def _raising_stream(message):
    async def _gen():
        raise ConnectionError(message)
        yield  # pragma: no cover - make this an async generator

    return _gen()


def test_telemetry_schedule_branch_appends_success(monkeypatch):
    monkeypatch.setattr(schedule_provider, "get_upcoming_events", Mock(return_value=[{"title": "X"}]))
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)
    telemetry_mock = Mock()
    monkeypatch.setattr(telemetry_store, "append_entry", telemetry_mock.append_entry)

    result = asyncio.run(JarvisService("voice.onnx").arun_text("what is on my calendar"))

    entry = _entry(telemetry_mock)
    assert result == "You have 1 events: X"
    assert entry.path == "schedule"
    assert entry.tools_fired == []
    assert entry.error is None
    assert entry.duration_ms >= 0
    assert entry.stt_ms is None


def test_telemetry_sdk_branch_appends_tools_fired(monkeypatch):
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)
    monkeypatch.setattr(
        sdk_backend.SDKBackend, "ask_stream",
        lambda self, prompt: _fake_ask_stream(["The vault says hello."]),
    )
    telemetry_mock = Mock()
    monkeypatch.setattr(telemetry_store, "append_entry", telemetry_mock.append_entry)

    service = JarvisService("voice.onnx")
    # Simulate the tools the stream would have recorded, so the entry reflects them.
    service._sdk_backend.last_tools_fired = ["mcp__vault__read", "mcp__vault__write"]
    asyncio.run(service.arun_text("what did I write?"))

    entry = _entry(telemetry_mock)
    assert entry.path == "sdk"
    assert entry.tools_fired == ["mcp__vault__read", "mcp__vault__write"]
    assert entry.error is None


def test_telemetry_sdk_branch_failure_records_class_name_and_propagates(monkeypatch):
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)
    monkeypatch.setattr(
        sdk_backend.SDKBackend,
        "ask_stream",
        lambda self, prompt: _raising_stream("the vault secret S3CR3T was exposed"),
    )
    telemetry_mock = Mock()
    monkeypatch.setattr(telemetry_store, "append_entry", telemetry_mock.append_entry)

    service = JarvisService("voice.onnx")
    # Simulate the tool the stream would have recorded before it failed.
    service._sdk_backend.last_tools_fired = ["mcp__vault__read"]
    try:
        asyncio.run(service.arun_text("what did I write?"))
    except ConnectionError as error:
        assert "S3CR3T" in str(error)
    else:
        raise AssertionError("expected ConnectionError to propagate")

    entry = _entry(telemetry_mock)
    assert entry.path == "sdk"
    assert entry.error == "ConnectionError"
    # Only the class name is recorded, never the exception message.
    assert "S3CR3T" not in str(entry.error)


def test_telemetry_failure_never_swallows_original_exception(monkeypatch, capsys):
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)
    monkeypatch.setattr(
        sdk_backend.SDKBackend,
        "ask_stream",
        lambda self, prompt: _raising_stream("boom"),
    )

    # Telemetry itself also raises: the turn's real error must still surface unchanged.
    def poisoned_append(entry, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry_store, "append_entry", poisoned_append)

    service = JarvisService("voice.onnx")
    try:
        asyncio.run(service.arun_text("what did I write?"))
    except ConnectionError as error:
        assert "boom" in str(error)
    except OSError:
        raise AssertionError("telemetry failure must not replace the original exception")
    else:
        raise AssertionError("expected ConnectionError to propagate")

    assert "telemetry write failed" in capsys.readouterr().err


def test_telemetry_failure_does_not_break_successful_turn(monkeypatch, capsys):
    monkeypatch.setattr(schedule_provider, "get_upcoming_events", Mock(return_value=[{"title": "Standup"}]))
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)

    def poisoned_append(entry, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry_store, "append_entry", poisoned_append)

    result = asyncio.run(JarvisService("voice.onnx").arun_text("what is on my calendar"))

    assert result == "You have 1 events: Standup"
    assert "telemetry write failed" in capsys.readouterr().err


def test_arun_once_passes_stt_ms_while_arun_text_does_not(monkeypatch):
    captured = {}

    async def record_answer(
        self,
        text,
        *,
        stt_ms=None,
        speak=True,
        turn_id=None,
        publish_processing=True,
    ):
        captured["stt_ms"] = stt_ms
        return "ok"

    monkeypatch.setattr(JarvisService, "_answer_with_turn", record_answer)

    # arun_once: stt_ms must be a concrete (non-None) measurement.
    monkeypatch.setattr(
        capture, "record_on_enter",
        Mock(return_value=np.array([0.0], dtype=np.float32)),
    )
    monkeypatch.setattr(stt, "transcribe", Mock(return_value="hello"))
    asyncio.run(JarvisService("voice.onnx").arun_once())
    assert captured["stt_ms"] is not None
    assert captured["stt_ms"] >= 0

    # arun_text: stt_ms must default to None.
    asyncio.run(JarvisService("voice.onnx").arun_text("hello"))
    assert captured["stt_ms"] is None


def test_aanswer_speak_false_on_schedule_branch_does_not_speak(monkeypatch):
    monkeypatch.setattr(
        schedule_provider, "get_upcoming_events", Mock(return_value=[{"title": "X"}])
    )
    monkeypatch.setattr(router, "route", lambda text: "schedule")
    speak_mock = Mock()
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))

    silent = asyncio.run(
        JarvisService("voice.onnx").aanswer("what is on my calendar", speak=False)
    )
    spoken = asyncio.run(
        JarvisService("voice.onnx").aanswer("what is on my calendar", speak=True)
    )

    assert silent == "You have 1 events: X"
    assert speak_mock.call_count == 1
    speak_mock.assert_called_once_with(spoken)
    assert silent == spoken


def test_aanswer_speak_false_on_sdk_branch_does_not_speak(monkeypatch):
    monkeypatch.setattr(router, "route", lambda text: "fallback")
    speak_mock = Mock()
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: speak_mock(text))
    monkeypatch.setattr(
        sdk_backend.SDKBackend, "ask_stream",
        lambda self, prompt: _fake_ask_stream(["The vault "]),
    )

    silent = asyncio.run(
        JarvisService("voice.onnx").aanswer("what did I write?", speak=False)
    )
    # Spoken control: same input with default/True must still speak.
    asyncio.run(
        JarvisService("voice.onnx").aanswer("what did I write?", speak=True)
    )

    assert silent == "The vault "
    assert speak_mock.call_count == 1


def test_telemetry_shape_is_independent_of_speak_schedule(monkeypatch):
    for speak in (True, False):
        monkeypatch.setattr(
            schedule_provider, "get_upcoming_events",
            Mock(return_value=[{"title": "X"}]),
        )
        monkeypatch.setattr(router, "route", lambda text: "schedule")
        monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)
        telemetry_mock = Mock()
        monkeypatch.setattr(telemetry_store, "append_entry", telemetry_mock.append_entry)

        asyncio.run(JarvisService("voice.onnx").aanswer("cal", speak=speak))

        entry = _entry(telemetry_mock)
        assert entry.path == "schedule"
        assert entry.tools_fired == []
        assert entry.error is None
        assert entry.duration_ms >= 0
        assert entry.stt_ms is None


def test_telemetry_shape_is_independent_of_speak_sdk(monkeypatch):
    shapes = []
    for speak in (True, False):
        monkeypatch.setattr(router, "route", lambda text: "fallback")
        monkeypatch.setattr(tts.Speaker, "say", lambda self, text: None)
        monkeypatch.setattr(
            sdk_backend.SDKBackend, "ask_stream",
            lambda self, prompt: _fake_ask_stream(["ok"]),
        )
        telemetry_mock = Mock()
        monkeypatch.setattr(telemetry_store, "append_entry", telemetry_mock.append_entry)

        service = JarvisService("voice.onnx")
        service._sdk_backend.last_tools_fired = ["mcp__vault__read"]
        asyncio.run(service.aanswer("q", speak=speak))

        entry = _entry(telemetry_mock)
        shapes.append((entry.path, entry.tools_fired, entry.error))

    assert shapes[0] == ("sdk", ["mcp__vault__read"], None)
    assert shapes[1] == shapes[0]


def _fake_audio_backend():
    async def ask_stream(self, prompt):
        yield f"echo: {prompt}"

    return ask_stream


def test_arun_audio_runs_stt_off_loop_and_returns_tuple(monkeypatch):
    worker_began = asyncio.Event()
    worker_releases = asyncio.Event()
    order = []

    def transcribe(audio):
        order.append(("stt", threading.get_ident()))
        worker_began.set()
        # asyncio.Event lacks a sync wait, so poll while staying off the loop.
        while not worker_releases.is_set():
            time.sleep(0.01)
        return "hello jarvis"

    monkeypatch.setattr(stt, "transcribe", transcribe)
    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", _fake_audio_backend())

    async def main():
        loop_id = threading.get_ident()
        task = asyncio.create_task(JarvisService("voice.onnx").arun_audio(np.zeros(160)))
        await worker_began.wait()
        order.append(("loop-advanced", loop_id))
        worker_releases.set()
        return await task

    transcript, response = asyncio.run(main())

    assert transcript == "hello jarvis"
    assert response == "echo: hello jarvis"
    assert isinstance(response, str)
    assert order[0][0] == "stt"
    assert order[1][0] == "loop-advanced"
    assert order[0][1] != order[1][1]  # stt ran off the event loop


def test_arun_audio_publishes_exact_status_and_turn_continuity(monkeypatch):
    calls, owner = _stub_runtime_status(monkeypatch)

    monkeypatch.setattr(stt, "transcribe", Mock(return_value="hello jarvis"))
    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", _fake_audio_backend())

    asyncio.run(JarvisService("voice.onnx").arun_audio(np.zeros(160)))

    publish_states = [record[2] for record in calls if record[0] == "publish"]
    fail_states = [record[-1] for record in calls if record[0] == "fail"]
    turn_ids = set()
    for record in calls:
        if record[0] == "publish":
            turn_ids.add(record[3])
        elif record[0] == "clear":
            turn_ids.add(record[2])
        elif record[0] == "fail":
            turn_ids.add(record[2])
    assert len(turn_ids) == 1
    assert all(record[1] == owner for record in calls)
    assert publish_states == ["processing"]
    assert fail_states == []


def test_arun_audio_passes_stt_ms_to_answer_with_turn(monkeypatch):
    captured = {}

    async def record_answer(
        self,
        text,
        *,
        stt_ms=None,
        speak=True,
        turn_id=None,
        publish_processing=True,
    ):
        captured.update(
            text=text,
            stt_ms=stt_ms,
            speak=speak,
            turn_id=turn_id,
            publish_processing=publish_processing,
        )
        return "response"

    monkeypatch.setattr(JarvisService, "_answer_with_turn", record_answer)
    monkeypatch.setattr(stt, "transcribe", Mock(return_value="hello jarvis"))

    asyncio.run(JarvisService("voice.onnx").arun_audio(np.zeros(160)))

    assert captured["text"] == "hello jarvis"
    assert captured["stt_ms"] is not None
    assert captured["stt_ms"] >= 0
    assert captured["speak"] is False
    assert isinstance(captured["turn_id"], str) and captured["turn_id"]
    assert captured["publish_processing"] is False


def test_arun_audio_publishes_no_speech_failure_without_routing(monkeypatch):
    calls, _owner = _stub_runtime_status(monkeypatch)
    routed = []

    def transcribe(audio):
        if routed:
            raise AssertionError("transcribe called twice")
        return "   "

    monkeypatch.setattr(stt, "transcribe", transcribe)
    monkeypatch.setattr(router, "route", lambda text: routed.append(text) or "sdk")
    ask_calls = []

    def ask_stream(self, prompt):
        ask_calls.append(prompt)
        return _fake_ask_stream(["nope"])

    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", ask_stream)

    with pytest.raises(Exception) as error:
        asyncio.run(JarvisService("voice.onnx").arun_audio(np.zeros(160)))

    from jarvis.orchestrator.service import NoSpeechDetectedError

    assert isinstance(error.value, NoSpeechDetectedError)
    assert routed == []
    assert ask_calls == []
    publish_states = [record[2] for record in calls if record[0] == "publish"]
    fail_states = [record[-1] for record in calls if record[0] == "fail"]
    turn_ids = {record[3] if record[0] == "publish" else record[2] for record in calls}
    assert publish_states[0] == "processing"
    assert "stt_failed" in (publish_states + fail_states)
    assert len(turn_ids) == 1


def test_arun_audio_re_raises_stt_error_and_marks_stt_failed(monkeypatch):
    calls, _owner = _stub_runtime_status(monkeypatch)
    boom = RuntimeError("mlx exploded")

    def transcribe(audio):
        raise boom

    monkeypatch.setattr(stt, "transcribe", transcribe)
    routed = []
    monkeypatch.setattr(router, "route", lambda text: routed.append(text) or "sdk")

    with pytest.raises(RuntimeError) as error:
        asyncio.run(JarvisService("voice.onnx").arun_audio(np.zeros(160)))

    assert error.value is boom
    assert routed == []
    publish_states = [record[2] for record in calls if record[0] == "publish"]
    fail_states = [record[-1] for record in calls if record[0] == "fail"]
    turn_ids = {record[3] if record[0] == "publish" else record[2] for record in calls}
    assert publish_states[0] == "processing"
    assert "stt_failed" in (publish_states + fail_states)
    assert len(turn_ids) == 1


def test_arun_audio_cancellation_before_dispatch_clears_only_matching_turn(monkeypatch):
    calls, _owner = _stub_runtime_status(monkeypatch)
    worker_began = threading.Event()
    worker_releases = threading.Event()

    def transcribe(audio):
        worker_began.set()
        worker_releases.wait()
        return "late transcript"

    monkeypatch.setattr(stt, "transcribe", transcribe)
    routed = []
    monkeypatch.setattr(router, "route", lambda text: routed.append(text) or "sdk")
    ask_calls = []

    def ask_stream(self, prompt):
        ask_calls.append(prompt)
        return _fake_ask_stream(["nope"])

    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", ask_stream)
    turn_id_holder = {}

    async def main():
        service = JarvisService("voice.onnx")
        task = asyncio.create_task(service.arun_audio(np.zeros(160)))
        await asyncio.to_thread(worker_began.wait)
        turn_id_holder["id"] = service._active_turn_id
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Let the already-running STT worker finish after cancellation.
        worker_releases.set()
        return service

    service = asyncio.run(main())

    assert routed == []
    assert ask_calls == []
    assert service._active_turn_id is None
    cleared = [record for record in calls if record[0] == "clear"]
    assert len(cleared) == 1
    assert cleared[0] == ("clear", _owner, turn_id_holder["id"])


def test_arun_audio_never_touches_capture_or_piper(monkeypatch):
    capture_calls = []
    piper_calls = []

    def record_on_enter(**kwargs):
        capture_calls.append(kwargs)
        raise AssertionError("capture must not happen for arun_audio")

    monkeypatch.setattr(capture, "record_on_enter", record_on_enter)
    monkeypatch.setattr(tts.Speaker, "say", lambda self, text: piper_calls.append(text))
    monkeypatch.setattr(stt, "transcribe", Mock(return_value="hello jarvis"))
    monkeypatch.setattr(sdk_backend.SDKBackend, "ask_stream", _fake_audio_backend())

    transcript, response = asyncio.run(
        JarvisService("voice.onnx").arun_audio(np.zeros(160))
    )

    assert capture_calls == []
    assert piper_calls == []
    assert transcript == "hello jarvis"
    assert response == "echo: hello jarvis"
