"""Push-to-talk audio capture utilities."""

from pathlib import Path
import wave

import numpy as np
import sounddevice as sd


def record_on_enter(
    samplerate: int = 16000,
    channels: int = 1,
    save_path: Path | None = None,
) -> np.ndarray:
    """Record from the default input device between two Enter keypresses.

    Audio is kept in memory and returned as a float32 array.  When ``save_path``
    is supplied, the same recording is additionally written as a PCM WAV file.
    """

    print("Press Enter to start recording...")
    input()

    frames: list[np.ndarray] = []

    def callback(indata, _frame_count, _time_info, _status) -> None:
        frames.append(np.asarray(indata, dtype=np.float32).copy())

    stream = sd.InputStream(
        samplerate=samplerate,
        channels=channels,
        dtype="float32",
        callback=callback,
    )
    started = False
    try:
        stream.start()
        started = True
    except BaseException:
        try:
            stream.close()
        except BaseException:
            pass
        raise
    try:
        print("Recording... press Enter to stop.")
        input()
    finally:
        try:
            if started:
                stream.stop()
        finally:
            stream.close()

    if frames:
        audio = np.ascontiguousarray(
            np.concatenate(frames, axis=0), dtype=np.float32
        )
    else:
        audio = np.empty((0,), dtype=np.float32) if channels == 1 else np.empty(
            (0, channels), dtype=np.float32
        )

    if channels == 1 and audio.ndim == 2:
        audio = np.ascontiguousarray(audio[:, 0], dtype=np.float32)

    if save_path is not None:
        _write_wav(audio, samplerate, channels, Path(save_path))

    return audio


def _write_wav(audio: np.ndarray, samplerate: int, channels: int, path: Path) -> None:
    """Write floating-point samples as 16-bit PCM without adding dependencies."""

    samples = np.clip(audio, -1.0, 1.0)
    pcm = (samples * 32767.0).astype(np.int16, copy=False)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(pcm.tobytes())
