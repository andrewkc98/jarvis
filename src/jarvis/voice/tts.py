"""Text-to-speech playback using Piper and an in-memory WAV buffer."""

import io
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from piper import PiperVoice


def speak(text: str, model_path: str | Path) -> None:
    """Synthesize *text* with Piper and play it through the default audio device."""
    voice = PiperVoice.load(str(model_path))

    chunks = iter(voice.synthesize(text))
    try:
        first_chunk = next(chunks)
    except StopIteration as exc:
        raise ValueError("Piper produced no audio chunks") from exc

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setframerate(first_chunk.sample_rate)
        wav_file.setsampwidth(first_chunk.sample_width)
        wav_file.setnchannels(first_chunk.sample_channels)
        wav_file.writeframes(first_chunk.audio_int16_bytes)
        for chunk in chunks:
            wav_file.writeframes(chunk.audio_int16_bytes)

    buf.seek(0)
    with wave.open(buf, "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16)
        channels = wav_file.getnchannels()
        if channels > 1:
            audio = audio.reshape(-1, channels)
        sd.play(audio, samplerate=wav_file.getframerate())
        sd.wait()
