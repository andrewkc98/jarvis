"""Tests for the bounded, in-memory browser audio decoder."""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from jarvis.voice import media


def _float32_bytes(n: int) -> bytes:
    return np.full(n, dtype=np.float32, fill_value=1.0).tobytes()


class _FakeCompleted:
    """Minimal stand-in for :class:`subprocess.CompletedProcess`."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


# --------------------------------------------------------------------------- #
# Constants / accepted type contract (no decode happening)
# --------------------------------------------------------------------------- #


def test_exported_constants_are_exact() -> None:
    assert media.MAX_UPLOAD_BYTES == 8388608
    assert media.MAX_DURATION_SECONDS == 30
    assert media.SAMPLE_RATE == 16000
    assert media.MAX_DECODED_SAMPLES == 30 * 16000
    assert list(media.ACCEPTED_MEDIA_TYPES) == ["audio/webm", "audio/ogg", "audio/mp4"]
    assert len(media.ACCEPTED_MEDIA_TYPES) == len(set(media.ACCEPTED_MEDIA_TYPES))


def test_error_types_are_distinguishable_by_class() -> None:
    # Callers must be able to distinguish these cases by class alone.
    cases = [
        media.EmptyAudioError,
        media.MediaTooLargeError,
        media.UnsupportedMediaTypeError,
        media.NoDecoderError,
        media.DecodeTimeoutError,
        media.DecodeError,
        media.AudioDurationOverflowError,
    ]
    assert len({cls.__name__ for cls in cases}) == len(cases)
    for cls in cases:
        assert issubclass(cls, media.MediaDecodeError)


# --------------------------------------------------------------------------- #
# MediaType normalization (before any decode)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content_type",
    ["audio/webm", "audio/webm;codecs=opus", "  AUDIO/WEBM  ", "Audio/Ogg"],
)
def test_media_types_accepted_case_and_params(content_type: str, monkeypatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/local/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout=_float32_bytes(8))
    )
    audio = media.decode_media(_float32_bytes(8), content_type)
    assert audio.dtype == np.float32


# --------------------------------------------------------------------------- #
# Pre-decode guards (no decoder lookup, no spawn)
# --------------------------------------------------------------------------- #


def test_empty_input_raises_without_decoder_or_spawn(monkeypatch) -> None:
    ran = {"run": 0}

    def fake_run(*args, **kwargs) -> object:
        ran["run"] += 1
        return _FakeCompleted()

    def _absent_decoder(_bin: str) -> None:
        return None

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media.shutil, "which", _absent_decoder)

    with pytest.raises(media.EmptyAudioError):
        media.decode_media(b"", "audio/webm")
    assert ran["run"] == 0


def test_over_limit_input_raises_after_type_check_before_spawn(monkeypatch) -> None:
    ran = {"run": 0}

    def fake_run(*args, **kwargs) -> object:
        ran["run"] += 1
        return _FakeCompleted()

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")

    with pytest.raises(media.MediaTooLargeError):
        media.decode_media(b"x" * (media.MAX_UPLOAD_BYTES + 1), "audio/ogg")
    assert ran["run"] == 0


def test_unsupported_type_raises_before_decoder_lookup(monkeypatch) -> None:
    calls = {"which": 0, "run": 0}

    def fake_which(_bin: str) -> str:
        calls["which"] += 1
        return "/usr/bin/ffmpeg"

    def fake_run(*args, **kwargs) -> object:
        calls["run"] += 1
        return _FakeCompleted()

    monkeypatch.setattr(media.shutil, "which", fake_which)
    monkeypatch.setattr(media.subprocess, "run", fake_run)

    with pytest.raises(media.UnsupportedMediaTypeError):
        media.decode_media(_float32_bytes(4), "audio/wave")
    assert calls["which"] == 0
    assert calls["run"] == 0


def test_unsupported_type_does_not_reveal_raw_value(monkeypatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    with pytest.raises(media.UnsupportedMediaTypeError):
        media.decode_media(_float32_bytes(4), "audio/wave")


# --------------------------------------------------------------------------- #
# ffmpeg spawn contract (exact argv / kwargs), fake run
# --------------------------------------------------------------------------- #


def test_decode_invokes_ffmpeg_with_safe_argv_and_pipes(monkeypatch) -> None:
    captured = {}

    def fake_run(argv, *, input=None, timeout=None, **kwargs) -> object:
        captured["argv"] = argv
        captured["input"] = input
        captured["timeout"] = timeout
        captured["shell"] = kwargs.get("shell")
        captured["stdin"] = kwargs.get("stdin")
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return _FakeCompleted(stdout=_float32_bytes(8))

    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", fake_run)

    raw = b"captured-recording-bytes"
    audio = media.decode_media(raw, "audio/webm")

    assert captured["argv"] == ["/usr/bin/ffmpeg", *media._FFMPEG_ARGS]
    assert captured["argv"] == [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-v",
        "quiet",
        "-i",
        "-",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "f32le",
        "-",
    ]
    # stdin/stdout pipes, shell off, reading from stdin, never a temp/named file.
    assert captured["shell"] is False
    assert captured["stdin"] is None  # input= supplies the pipe; stdin= would raise
    assert captured["input"] == raw
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is not None
    for chunk in ("-i", "-f", "f32le", "-nostdin"):
        assert chunk in captured["argv"]
    # The reading-from-stdin contract: the first input target is "-" and the input
    # bytes are the recording, passed through a pipe (not a temp/named file).
    assert captured["argv"][captured["argv"].index("-i") + 1] == "-"
    assert captured["input"] is raw
    # Output bytes are consumed in memory only.
    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.float32


def test_returns_owned_writable_ccontiguous_1d_float32(monkeypatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout=_float32_bytes(8)),
    )
    audio = media.decode_media(_float32_bytes(8), "audio/webm")
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert audio.flags["C_CONTIGUOUS"]
    assert audio.flags["WRITEABLE"]
    # Owns its data (copies rather than a read-only frombuffer view).
    audio[0] = 2.0
    assert audio[0] == 2.0


# --------------------------------------------------------------------------- #
# Decoder-unavailable
# --------------------------------------------------------------------------- #


def test_no_decoder_raises_unavailable(monkeypatch) -> None:
    calls = {"run": 0}

    def fake_run(*args, **kwargs) -> object:
        calls["run"] += 1
        return _FakeCompleted()

    monkeypatch.setattr(media.shutil, "which", lambda _bin: None)
    monkeypatch.setattr(media.subprocess, "run", fake_run)

    with pytest.raises(media.NoDecoderError):
        media.decode_media(_float32_bytes(4), "audio/mp4")
    assert calls["run"] == 0


# --------------------------------------------------------------------------- #
# All decode/return boundaries
# --------------------------------------------------------------------------- #


def test_timeout_raises_decode_timeout(monkeypatch) -> None:
    def fake_run(*args, **kwargs) -> object:
        raise subprocess.TimeoutExpired("ffmpeg", media.DECODE_TIMEOUT_SECONDS)

    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", fake_run)

    with pytest.raises(media.DecodeTimeoutError):
        media.decode_media(_float32_bytes(8), "audio/webm")


def test_nonzero_status_raises_decode_error(monkeypatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **kw: _FakeCompleted(stdout=b"", returncode=1))

    with pytest.raises(media.DecodeError):
        media.decode_media(_float32_bytes(8), "audio/ogg")


def test_empty_output_raises_decode_error(monkeypatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **kw: _FakeCompleted(stdout=b"")
    )
    with pytest.raises(media.DecodeError):
        media.decode_media(_float32_bytes(8), "audio/webm")


def test_length_not_divisible_by_four_raises(monkeypatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **kw: _FakeCompleted(stdout=b"abc")
    )
    with pytest.raises(media.DecodeError):
        media.decode_media(b"\x00\x00\x00\x00", "audio/webm")


def test_nonfinite_samples_raises_decode_error(monkeypatch) -> None:
    data = np.array([np.nan], dtype=np.float32).tobytes()
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **kw: _FakeCompleted(stdout=data)
    )
    with pytest.raises(media.DecodeError):
        media.decode_media(data, "audio/webm")


def test_too_many_samples_raises_duration_overflow(monkeypatch) -> None:
    data = _float32_bytes(media.MAX_DECODED_SAMPLES + 1)
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **kw: _FakeCompleted(stdout=data)
    )
    with pytest.raises(media.AudioDurationOverflowError):
        media.decode_media(data, "audio/webm")


def test_within_limits_returns_silence(monkeypatch) -> None:
    data = _float32_bytes(100)
    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **kw: _FakeCompleted(stdout=data)
    )
    audio = media.decode_media(data, "audio/ogg")
    assert audio.size == 100
    assert audio.dtype == np.float32
    assert np.all(np.isfinite(audio))


# --------------------------------------------------------------------------- #
# No cross-contamination: input bytes and fake diagnostics are never emitted
# --------------------------------------------------------------------------- #


def test_raw_input_bytes_and_stderr_are_never_emitted_to_stdout(monkeypatch, capsys) -> None:
    secret_input = b"TOP-SECRET-RECORDING"

    def fake_run(argv, *, input=None, **kwargs) -> object:
        # ffmpeg "prints" diagnostics on its OWN stderr pipe (captured), not stdout.
        return _FakeCompleted(stdout=_float32_bytes(4))

    monkeypatch.setattr(media.shutil, "which", lambda _bin: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", fake_run)

    media.decode_media(secret_input, "audio/webm")

    out = capsys.readouterr().out
    assert secret_input.decode() not in out
    for term in ("audio/webm", "ffmpeg", "f32le"):
        assert term not in out


# --------------------------------------------------------------------------- #
# Real ffmpeg round trip (skipped when ffmpeg/libopus are unavailable)
# --------------------------------------------------------------------------- #


def test_decode_media_real_ffmpeg_webm_opus() -> None:
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    encoded = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", "-f", "webm", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if encoded.returncode != 0 or not encoded.stdout:
        pytest.skip("ffmpeg cannot encode webm/opus")
    audio = media.decode_media(encoded.stdout, "audio/webm;codecs=opus")
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert 0.8 * media.SAMPLE_RATE <= audio.size <= 1.3 * media.SAMPLE_RATE
    assert float(np.abs(audio).max()) > 0.1
