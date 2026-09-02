"""Text-to-speech playback using Piper and an in-memory WAV buffer."""

import io
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from piper import PiperVoice


class SentenceBuffer:
    """Buffers streamed text and yields complete sentences at sentence-boundary
    punctuation (`.`, `!`, `?`, or a newline)."""

    _TERMINATORS = (".", "!", "?", "\n")

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        """Add a text delta; return zero or more complete sentences now available,
        in order."""
        self._buffer += chunk
        sentences = []
        while True:
            cut = self._find_boundary(self._buffer)
            if cut is None:
                break
            candidate, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:]
            if candidate:
                sentences.append(candidate)
        return sentences

    def flush(self) -> str | None:
        """Return and clear any trailing buffered text that never reached a terminal
        boundary. Returns None if nothing is buffered."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining or None

    def _find_boundary(self, text: str) -> int | None:
        for index, character in enumerate(text):
            if character in self._TERMINATORS:
                return index + 1
        return None


class Speaker:
    """Reuses one Piper voice, loaded lazily on first use, across multiple say() calls."""

    def __init__(self, model_path: str | Path) -> None:
        self._model_path = model_path
        self._voice: PiperVoice | None = None

    def say(self, text: str) -> None:
        """Synthesize *text* with Piper and play it through the default audio device."""
        if self._voice is None:
            self._voice = PiperVoice.load(str(self._model_path))

        chunks = iter(self._voice.synthesize(text))
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


def speak(text: str, model_path: str | Path) -> None:
    """Synthesize *text* with Piper and play it through the default audio device."""
    Speaker(model_path).say(text)
