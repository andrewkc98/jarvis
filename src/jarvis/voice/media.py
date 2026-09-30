"""Bounded, in-memory browser audio decoder.

This module is the only place browser audio is converted on disk-free memory:
raw recording bytes are streamed straight into a single ``ffmpeg`` invocation
and the decoded waveform is rebuilt from :mod:`numpy`.  Nothing is written to a
temporary or named file and neither the raw recording, ``ffmpeg`` stderr, nor
any exception text is ever printed to stdout, so captured audio stays in
memory and off the record.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

import numpy as np


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Maximum upload size in bytes (8 MiB).
MAX_UPLOAD_BYTES = 8388608

#: Longest recording allowed in seconds.
MAX_DURATION_SECONDS = 30

#: Resampled output sample rate used for STT.
SAMPLE_RATE = 16000

#: Largest decoded sample count allowed: MAX_DURATION_SECONDS * SAMPLE_RATE.
MAX_DECODED_SAMPLES = MAX_DURATION_SECONDS * SAMPLE_RATE

#: Ordered set of accepted normalized media types.
ACCEPTED_MEDIA_TYPES = ("audio/webm", "audio/ogg", "audio/mp4")

#: ffmpeg invocation timeout used when decoding a recording.
DECODE_TIMEOUT_SECONDS = 5.0

#: argv passed to ffmpeg on every decode (fixed, parameter-free).
_FFMPEG_ARGS = (
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
)

logger = logging.getLogger("jarvis.voice.media")


# --------------------------------------------------------------------------- #
# Typed errors (class only, never message text)
# --------------------------------------------------------------------------- #


class MediaDecodeError(Exception):
    """Base class for every media-decode failure."""


class EmptyAudioError(MediaDecodeError):
    """The uploaded recording contained zero bytes."""


class MediaTooLargeError(MediaDecodeError):
    """The uploaded recording exceeded :data:`MAX_UPLOAD_BYTES`."""


class UnsupportedMediaTypeError(MediaDecodeError):
    """The Content-Type media type was not offered by the browser."""


class NoDecoderError(MediaDecodeError):
    """Required ``ffmpeg`` decoder was not discovered on the system."""


class DecodeTimeoutError(MediaDecodeError):
    """Decoding exceeded :data:`DECODE_TIMEOUT_SECONDS`."""


class DecodeError(MediaDecodeError):
    """ffmpeg failed, produced no usable output, or yielded bad samples."""


class AudioDurationOverflowError(MediaDecodeError):
    """Decoded more than :data:`MAX_DECODED_SAMPLES` samples."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _normalize_media_type(content_type: str) -> str:
    """Return the normalized media type or raise :class:`UnsupportedMediaTypeError`.

    The media type is taken before any ``;`` parameters and compared
    case-insensitively after stripping surrounding whitespace.
    """

    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in ACCEPTED_MEDIA_TYPES:
        raise UnsupportedMediaTypeError(media_type)
    return media_type


def _decode_ffmpeg(argv: tuple[str, ...], raw: bytes) -> np.ndarray:
    """Decode raw recording bytes with a single ffmpeg invocation.

    ffmpeg reads the recording from stdin and writes raw little-endian
    float32 to stdout; both use pipes and there is no temporary file.
    """

    try:
        completed = subprocess.run(
            list(argv),
            input=raw,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=DECODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("media decode timed out after %s seconds", DECODE_TIMEOUT_SECONDS)
        raise DecodeTimeoutError("media decode timed out") from exc
    except Exception as exc:  # noqa: BLE001 - reported as a decode failure
        logger.error("media decode failed to start the decoder")
        raise DecodeError("media decode did not complete") from exc

    if completed.returncode != 0:
        logger.error("media decode reported a non-zero status")
        raise DecodeError("media decode did not complete")

    raw_out = completed.stdout
    if not raw_out:
        raise DecodeError("media decode produced no output")
    if len(raw_out) % 4 != 0:
        raise DecodeError("media decode produced malformed output")

    samples = np.ascontiguousarray(np.asarray(np.frombuffer(bytes(raw_out), dtype=np.float32)))
    # ``frombuffer`` yields a read-only view; ``np.ascontiguousarray`` returns a
    # view of it, so copy to get a writable array.
    audio = np.ascontiguousarray(samples.copy()).astype(np.float32, copy=False)
    assert audio.flags["WRITEABLE"]
    assert audio.flags["C_CONTIGUOUS"] and audio.ndim == 1

    if not bool(np.all(np.isfinite(audio))):
        raise DecodeError("media decode produced non-finite samples")
    if audio.size > MAX_DECODED_SAMPLES:
        raise AudioDurationOverflowError("browser audio too long")
    return audio


def decode_media(input_bytes: bytes, content_type: str) -> np.ndarray:
    """Decode a browser recording into an owned ``float32`` waveform.

    ``input_bytes`` are consumed only from memory; nothing is ever written to
    disk and nothing is printed to stdout.  Returns a one-dimensional,
    C-contiguous, writable :class:`numpy.float32` array sampled at
    :data:`SAMPLE_RATE`.
    """

    _normalize_media_type(content_type)

    raw = input_bytes
    if len(raw) == 0:
        raise EmptyAudioError("uploaded audio was empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise MediaTooLargeError("uploaded audio exceeded the size limit")

    decoder = shutil.which("ffmpeg")
    if decoder is None:
        raise NoDecoderError("ffmpeg decoder not available")

    audio = _decode_ffmpeg(_FFMPEG_ARGS, raw)
    return audio
