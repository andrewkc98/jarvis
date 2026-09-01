"""Tests for in-memory Piper text-to-speech playback."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from jarvis.voice import tts


def test_speak_uses_synthesize_and_plays_in_memory_wav(monkeypatch, tmp_path):
    calls = []
    first = np.array([0, 1000], dtype=np.int16)
    second = np.array([-1000, 250], dtype=np.int16)

    class FakeVoice:
        def synthesize(self, text):
            calls.append(text)
            yield SimpleNamespace(
                sample_rate=22050,
                sample_width=2,
                sample_channels=1,
                audio_int16_bytes=first.tobytes(),
            )
            yield SimpleNamespace(
                sample_rate=22050,
                sample_width=2,
                sample_channels=1,
                audio_int16_bytes=second.tobytes(),
            )

    monkeypatch.setattr(tts.PiperVoice, "load", lambda path: FakeVoice())
    played = {}
    monkeypatch.setattr(
        tts.sd,
        "play",
        lambda audio, samplerate: played.update(
            audio=np.array(audio, copy=True), samplerate=samplerate
        ),
    )
    waited = []
    monkeypatch.setattr(tts.sd, "wait", lambda: waited.append(True))

    tts.speak("hello", tmp_path / "voice.onnx")

    assert len(calls) == 1
    assert calls[0] == "hello"
    assert played["samplerate"] == 22050
    np.testing.assert_array_equal(played["audio"], np.concatenate((first, second)))
    assert waited == [True]
    assert not list(Path(tmp_path).glob("*.wav"))


def test_speak_rejects_empty_chunk_iterable_without_playback(monkeypatch, tmp_path):
    class FakeVoice:
        def synthesize(self, text):
            if text != "quiet":
                raise AssertionError("unexpected text")
            return iter(())

    monkeypatch.setattr(tts.PiperVoice, "load", lambda path: FakeVoice())
    played = []
    monkeypatch.setattr(tts.sd, "play", lambda *args, **kwargs: played.append(args))

    import pytest

    with pytest.raises(ValueError, match="no audio chunks"):
        tts.speak("quiet", tmp_path / "voice.onnx")

    assert played == []
    assert not list(Path(tmp_path).glob("*.wav"))
