from unittest.mock import Mock
import sys
from types import ModuleType

import numpy as np
import pytest

from jarvis.voice import stt


def _install_mlx_whisper_stub(monkeypatch, transcribe_mock):
    mlx_whisper_stub = ModuleType("mlx_whisper")
    mlx_whisper_stub.transcribe = transcribe_mock
    monkeypatch.setitem(sys.modules, "mlx_whisper", mlx_whisper_stub)


def test_transcribe_passes_mono_float32_waveform_and_strips_text(monkeypatch):
    transcribe_mock = Mock(return_value={"text": "  hello world  "})
    _install_mlx_whisper_stub(monkeypatch, transcribe_mock)
    audio = np.array([0.1, -0.2], dtype=np.float32)

    result = stt.transcribe(audio)

    assert result == "hello world"
    transcribe_mock.assert_called_once()
    passed_audio, = transcribe_mock.call_args.args
    assert passed_audio is audio
    transcribe_mock.assert_called_once_with(
        audio, path_or_hf_repo="mlx-community/whisper-large-v3-turbo"
    )


def test_transcribe_downmixes_stereo_to_contiguous_mono(monkeypatch):
    transcribe_mock = Mock(return_value={"text": "ok"})
    _install_mlx_whisper_stub(monkeypatch, transcribe_mock)
    audio = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float64)

    stt.transcribe(audio, model_repo="custom/model")

    passed_audio = transcribe_mock.call_args.args[0]
    np.testing.assert_array_equal(passed_audio, np.array([0.5, 0.5], dtype=np.float32))
    assert passed_audio.dtype == np.float32
    assert passed_audio.ndim == 1
    assert passed_audio.flags.c_contiguous
    transcribe_mock.assert_called_once_with(
        passed_audio, path_or_hf_repo="custom/model"
    )


def test_transcribe_rejects_non_16khz_without_model_call(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)

    with pytest.raises(ValueError, match="16000"):
        stt.transcribe(np.zeros(4), samplerate=8000)



@pytest.mark.parametrize("audio", [np.array(1.0), np.zeros((2, 2, 2))])
def test_transcribe_rejects_invalid_waveform_rank(monkeypatch, audio):
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)

    with pytest.raises(ValueError, match="one- or two-dimensional"):
        stt.transcribe(audio)
