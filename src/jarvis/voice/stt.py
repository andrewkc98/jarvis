"""Speech-to-text wrapper around MLX Whisper."""

import numpy as np


def transcribe(
    audio: np.ndarray,
    samplerate: int = 16000,
    model_repo: str = "mlx-community/whisper-large-v3-turbo",
) -> str:
    """Transcribe an in-memory audio array with an MLX Whisper model.

    Audio is expected to be a NumPy array sampled at ``samplerate``.  MLX
    Whisper accepts the array directly, so no temporary audio file is needed.
    """
    processed_audio = np.asarray(audio, dtype=np.float32)
    if samplerate != 16000:
        raise ValueError("MLX Whisper array input requires a 16000 Hz sample rate")
    if processed_audio.ndim == 0 or processed_audio.ndim > 2:
        raise ValueError("audio must be a one- or two-dimensional waveform")
    if processed_audio.ndim == 2:
        processed_audio = np.ascontiguousarray(
            processed_audio.mean(axis=1, dtype=np.float32), dtype=np.float32
        )

    import mlx_whisper

    result = mlx_whisper.transcribe(
        processed_audio, path_or_hf_repo=model_repo
    )
    return result["text"].strip()
