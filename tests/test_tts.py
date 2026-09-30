"""Tests for in-memory Piper text-to-speech playback."""

import io
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.voice import tts
from jarvis.voice.tts import SentenceBuffer


def _chunk(samples, sample_rate=22050, sample_width=2, sample_channels=1):
    return SimpleNamespace(
        sample_rate=sample_rate,
        sample_width=sample_width,
        sample_channels=sample_channels,
        audio_int16_bytes=np.array(samples, dtype=np.int16).tobytes(),
    )


def _speaker_with_chunks(monkeypatch, tmp_path, chunks):
    monkeypatch.setattr(tts.PiperVoice, "load", lambda path: _fake_voice(chunks))
    return tts.Speaker(tmp_path / "voice.onnx")


def _fake_voice(chunks):
    class FakeVoice:
        def synthesize(self, text):
            return iter(chunks)

    return FakeVoice()


def test_sentence_buffer_splits_punctuation_across_feed_calls():
    buffer = SentenceBuffer()
    assert buffer.feed("Hello wor") == []
    assert buffer.feed("ld. Bye") == ["Hello world."]
    assert buffer.flush() == "Bye"


def test_sentence_buffer_returns_multiple_sentences_from_one_chunk():
    buffer = SentenceBuffer()
    assert buffer.feed("Hi. Bye. ") == ["Hi.", "Bye."]
    assert buffer.flush() is None


def test_sentence_buffer_splits_on_newline():
    buffer = SentenceBuffer()
    assert buffer.feed("Line one\nLine two") == ["Line one"]
    assert buffer.flush() == "Line two"


def test_sentence_buffer_flush_returns_none_when_empty():
    buffer = SentenceBuffer()
    assert buffer.feed("no terminator") == []
    assert buffer.flush() == "no terminator"
    assert buffer.flush() is None


def test_sentence_buffer_empty_feed_is_a_no_op():
    buffer = SentenceBuffer()
    assert buffer.feed("") == []
    assert buffer.feed("Hi.") == ["Hi."]


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

    with pytest.raises(ValueError, match="no audio chunks"):
        tts.speak("quiet", tmp_path / "voice.onnx")

    assert played == []
    assert not list(Path(tmp_path).glob("*.wav"))


def test_speaker_loads_voice_once_across_multiple_say_calls(monkeypatch, tmp_path):
    load_calls = []
    sample = SimpleNamespace(
        sample_rate=22050,
        sample_width=2,
        sample_channels=1,
        audio_int16_bytes=np.array([0, 1000], dtype=np.int16).tobytes(),
    )

    class FakeVoice:
        def synthesize(self, text):
            yield sample

    def fake_load(path):
        load_calls.append(path)
        return FakeVoice()

    monkeypatch.setattr(tts.PiperVoice, "load", fake_load)
    monkeypatch.setattr(tts.sd, "play", lambda audio, samplerate: None)
    monkeypatch.setattr(tts.sd, "wait", lambda: None)

    speaker = tts.Speaker(tmp_path / "voice.onnx")
    speaker.say("hello")
    speaker.say("again")

    assert len(load_calls) == 1


def test_synthesize_wav_returns_valid_wav_header_and_frames(monkeypatch, tmp_path):
    first = np.array([0, 1000], dtype=np.int16)
    second = np.array([-1000, 250], dtype=np.int16)
    speaker = _speaker_with_chunks(
        monkeypatch, tmp_path, [_chunk(first), _chunk(second)]
    )

    wav_bytes = speaker.synthesize_wav("hello")

    assert isinstance(wav_bytes, bytes)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 22050
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnchannels() == 1
        frames = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
    np.testing.assert_array_equal(frames, np.concatenate((first, second)))
    assert not list(Path(tmp_path).glob("*.wav"))


def test_synthesize_wav_orders_multi_chunk_assembly_and_header_is_first_chunk_only(
    monkeypatch, tmp_path
):
    samples_by_chunk = [
        np.array([1, 2], dtype=np.int16),
        np.array([3, 4], dtype=np.int16),
        np.array([5, 6], dtype=np.int16),
    ]
    speaker = _speaker_with_chunks(
        monkeypatch, tmp_path, [_chunk(chunk) for chunk in samples_by_chunk]
    )

    wav_bytes = speaker.synthesize_wav("hello")

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        frames = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
    np.testing.assert_array_equal(
        frames, np.concatenate(samples_by_chunk)
    )


def test_synthesize_wav_accepts_consistent_metadata_across_chunks(
    monkeypatch, tmp_path
):
    speaker = _speaker_with_chunks(
        monkeypatch,
        tmp_path,
        [
            _chunk(np.array([1, 2], dtype=np.int16), sample_rate=16000, sample_channels=2),
            _chunk(np.array([3, 4], dtype=np.int16), sample_rate=16000, sample_channels=2),
        ],
    )

    wav_bytes = speaker.synthesize_wav("hello")

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 2
        assert wav_file.getnframes() == 2


@pytest.mark.parametrize(
    "chunks",
    [
        [_chunk(np.array([1], dtype=np.int16), sample_rate=0)],
        [_chunk(np.array([1], dtype=np.int16), sample_rate=-44100)],
        [_chunk(np.array([1], dtype=np.int16), sample_width=0)],
        [_chunk(np.array([1], dtype=np.int16), sample_width=-2)],
        [_chunk(np.array([1], dtype=np.int16), sample_channels=0)],
        [_chunk(np.array([1], dtype=np.int16), sample_channels=-1)],
    ],
)
def test_synthesize_wav_rejects_nonpositive_first_chunk_metadata(monkeypatch, tmp_path, chunks):
    speaker = _speaker_with_chunks(monkeypatch, tmp_path, chunks)
    with pytest.raises(ValueError):
        speaker.synthesize_wav("hello")
    assert not list(Path(tmp_path).glob("*.wav"))


def test_synthesize_wav_rejects_inconsistent_sample_rate(
    monkeypatch, tmp_path
):
    speaker = _speaker_with_chunks(
        monkeypatch,
        tmp_path,
        [
            _chunk(np.array([1], dtype=np.int16), sample_rate=22050),
            _chunk(np.array([2], dtype=np.int16), sample_rate=16000),
        ],
    )
    with pytest.raises(ValueError, match="inconsistent"):
        speaker.synthesize_wav("hello")
    assert not list(Path(tmp_path).glob("*.wav"))


def test_synthesize_wav_rejects_inconsistent_sample_width(monkeypatch, tmp_path):
    speaker = _speaker_with_chunks(
        monkeypatch,
        tmp_path,
        [
            _chunk(np.array([1], dtype=np.int16), sample_width=2),
            _chunk(np.array([2], dtype=np.int16), sample_width=4),
        ],
    )
    with pytest.raises(ValueError, match="inconsistent"):
        speaker.synthesize_wav("hello")


def test_synthesize_wav_rejects_inconsistent_sample_channels(monkeypatch, tmp_path):
    speaker = _speaker_with_chunks(
        monkeypatch,
        tmp_path,
        [
            _chunk(np.array([1], dtype=np.int16), sample_channels=1),
            _chunk(np.array([2], dtype=np.int16), sample_channels=2),
        ],
    )
    with pytest.raises(ValueError, match="inconsistent"):
        speaker.synthesize_wav("hello")


def test_synthesize_wav_rejects_empty_chunk_iterable_without_partial_wav(monkeypatch, tmp_path):
    speaker = _speaker_with_chunks(monkeypatch, tmp_path, [])
    with pytest.raises(ValueError, match="no audio chunks"):
        speaker.synthesize_wav("quiet")
    assert not list(Path(tmp_path).glob("*.wav"))


def test_synthesize_wav_rejects_chunks_with_zero_audio_bytes(monkeypatch, tmp_path):
    speaker = _speaker_with_chunks(
        monkeypatch,
        tmp_path,
        [
            _chunk(np.array([], dtype=np.int16)),
            _chunk(np.array([], dtype=np.int16)),
        ],
    )
    with pytest.raises(ValueError, match="no audio chunks"):
        speaker.synthesize_wav("quiet")


def test_synthesize_wav_uses_lazy_voice_load(monkeypatch, tmp_path):
    load_calls = []

    class FakeVoice:
        def synthesize(self, text):
            return iter([_chunk(np.array([0, 1], dtype=np.int16))])

    def fake_load(path):
        load_calls.append(path)
        return FakeVoice()

    monkeypatch.setattr(tts.PiperVoice, "load", fake_load)
    speaker = tts.Speaker(tmp_path / "voice.onnx")

    assert load_calls == []
    speaker.synthesize_wav("hello")
    speaker.synthesize_wav("again")

    assert load_calls == [str(tmp_path / "voice.onnx")]


def test_synthesize_wav_consume_all_chunks_before_returning(monkeypatch, tmp_path):
    consumed = []
    generated = [
        _chunk(np.array([1], dtype=np.int16)),
        _chunk(np.array([2], dtype=np.int16)),
    ]

    class LazyVoice:
        def synthesize(self, text):
            for chunk in generated:
                consumed.append(chunk)
                yield chunk

    monkeypatch.setattr(tts.PiperVoice, "load", lambda path: LazyVoice())
    speaker = tts.Speaker(tmp_path / "voice.onnx")

    wav_bytes = speaker.synthesize_wav("hello")

    assert [chunk.audio_int16_bytes for chunk in consumed] == [
        b"\x01\x00",
        b"\x02\x00",
    ]
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        frames = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
    np.testing.assert_array_equal(frames, np.array([1, 2], dtype=np.int16))


def test_say_still_plays_after_synthesize_wav_via_in_memory_reopen(monkeypatch, tmp_path):
    first = np.array([0, 1000], dtype=np.int16)
    second = np.array([-1000, 250], dtype=np.int16)
    speaker = _speaker_with_chunks(
        monkeypatch, tmp_path, [_chunk(first), _chunk(second)]
    )
    played = {}
    monkeypatch.setattr(
        tts.sd,
        "play",
        lambda audio, samplerate: played.update(
            audio=np.array(audio, copy=True), samplerate=samplerate
        ),
    )
    monkeypatch.setattr(tts.sd, "wait", lambda: played.setdefault("waited", True))

    speaker.say("hello")

    assert played["samplerate"] == 22050
    np.testing.assert_array_equal(played["audio"], np.concatenate((first, second)))
    assert played["waited"] is True
    assert not list(Path(tmp_path).glob("*.wav"))


def test_say_multichannel_reshaping_preserved(monkeypatch, tmp_path):
    speaker = _speaker_with_chunks(
        monkeypatch,
        tmp_path,
        [_chunk(np.array([1, 2, 3, 4], dtype=np.int16), sample_channels=2)],
    )
    played = {}
    monkeypatch.setattr(
        tts.sd,
        "play",
        lambda audio, samplerate: played.update(
            audio=np.array(audio, copy=True), samplerate=samplerate
        ),
    )
    monkeypatch.setattr(tts.sd, "wait", lambda: None)

    speaker.say("hello")

    assert played["audio"].shape == (2, 2)
    np.testing.assert_array_equal(
        played["audio"], np.array([[1, 2], [3, 4]], dtype=np.int16)
    )
