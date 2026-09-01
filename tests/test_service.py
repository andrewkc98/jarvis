from unittest.mock import Mock

import numpy as np

from jarvis.orchestrator.service import JarvisService
from jarvis.voice import capture, stt, tts


def test_run_text_skips_capture_and_transcription(monkeypatch):
    capture_mock = Mock()
    stt_mock = Mock()
    speak_mock = Mock()
    monkeypatch.setattr(capture, "record_on_enter", capture_mock)
    monkeypatch.setattr(stt, "transcribe", stt_mock)
    monkeypatch.setattr(tts, "speak", speak_mock)

    service = JarvisService(voice_model_path="voice.onnx")
    result = service.run_text("hello world")

    assert result == "You said: hello world"
    capture_mock.assert_not_called()
    stt_mock.assert_not_called()
    speak_mock.assert_called_once_with(
        "You said: hello world", model_path="voice.onnx"
    )


def test_run_once_calls_capture_stt_and_tts_in_order(monkeypatch):
    audio = np.array([0.1, 0.2], dtype=np.float32)
    events = []
    capture_mock = Mock(side_effect=lambda: (events.append("capture"), audio)[1])
    stt_mock = Mock(
        side_effect=lambda value: (events.append(("stt", value)), "testing one two three")[1]
    )
    speak_mock = Mock(side_effect=lambda *args, **kwargs: events.append(("tts", args, kwargs)))
    monkeypatch.setattr(capture, "record_on_enter", capture_mock)
    monkeypatch.setattr(stt, "transcribe", stt_mock)
    monkeypatch.setattr(tts, "speak", speak_mock)

    service = JarvisService(voice_model_path="voice.onnx")
    result = service.run_once()

    assert result == "You said: testing one two three"
    assert events == [
        "capture",
        ("stt", audio),
        ("tts", ("You said: testing one two three",), {"model_path": "voice.onnx"}),
    ]
    capture_mock.assert_called_once_with()
    stt_mock.assert_called_once_with(audio)
    speak_mock.assert_called_once_with(
        "You said: testing one two three", model_path="voice.onnx"
    )
