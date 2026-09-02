import asyncio
from unittest.mock import Mock

import numpy as np

from jarvis.orchestrator.service import JarvisService, _speak_stream
from jarvis.orchestrator import router, sdk_backend
from jarvis.providers import schedule_provider
from jarvis.voice import capture, stt, tts


async def _fake_ask_stream(chunks):
    for chunk in chunks:
        yield chunk


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
    assert speaker.calls == []


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
        Mock(side_effect=lambda: (events.append("capture"), audio)[1]),
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
