"""Application service connecting audio capture, transcription, and speech."""

import asyncio
import sys
import time
from collections.abc import AsyncIterator
import numpy as np
from uuid import uuid4

from jarvis.voice import capture, stt
from jarvis.orchestrator import router, sdk_backend
from jarvis.providers import schedule_provider
from jarvis.runtime import status as runtime_status
from jarvis.telemetry import store
from jarvis.telemetry.store import TelemetryEntry
from jarvis.voice.tts import SentenceBuffer
from jarvis.voice.tts import Speaker


class _TTSDispatchFailure(Exception):
    """Internal marker used to map a playback error without replacing it."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


class _SDKDispatchFailure(Exception):
    """Internal marker used to map a stream error without replacing it."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


async def _speak_stream(
    chunks: AsyncIterator[str],
    speaker,
    *,
    on_speaking=None,
    on_speech_error=None,
) -> str:
    """Speak streamed text sentence-by-sentence as it arrives; return the full text.

    A producer drains *chunks* and feeds a SentenceBuffer; the one consumer speaks
    completed sentences off the event loop via asyncio.to_thread, so a speaker.say()
    call in flight never blocks the producer from continuing to drain *chunks*. The
    returned text is the raw concatenation of every chunk received, tracked
    independently of how it was split into spoken sentences.

    speaker=None skips TTS entirely and just concatenates the streamed text — used
    when the caller doesn't want this turn spoken aloud.
    """
    if speaker is None:
        received = []
        async for chunk in chunks:
            received.append(chunk)
        return "".join(received)

    buffer = SentenceBuffer()
    queue: asyncio.Queue = asyncio.Queue()
    received: list[str] = []
    producer_error: list[BaseException] = []

    async def produce() -> None:
        try:
            async for chunk in chunks:
                received.append(chunk)
                for sentence in buffer.feed(chunk):
                    await queue.put(sentence)
        except BaseException as exc:
            producer_error.append(exc)
        finally:
            trailing = buffer.flush()
            if trailing:
                await queue.put(trailing)
            await queue.put(None)

    loop = asyncio.get_running_loop()

    def consume() -> None:
        """Runs on one dedicated worker thread for the entire turn: every
        speaker.say() call, and the native audio stream each one opens and
        closes, happens on this single thread from the first sentence through
        the last — not a fresh ambient thread-pool thread per sentence."""
        speaking_published = False
        while True:
            sentence = asyncio.run_coroutine_threadsafe(queue.get(), loop).result()
            if sentence is None:
                return
            if sentence and on_speaking is not None and not speaking_published:
                on_speaking()
                speaking_published = True
            try:
                speaker.say(sentence)
            except Exception as exc:
                if on_speech_error is not None:
                    on_speech_error(exc)
                raise

    producer_task = asyncio.create_task(produce())
    try:
        await asyncio.to_thread(consume)
    except BaseException:
        producer_task.cancel()
        raise
    finally:
        try:
            await producer_task
        except asyncio.CancelledError:
            pass

    if producer_error:
        raise producer_error[0]
    return "".join(received)


class NoSpeechDetectedError(Exception):
    """STT produced no usable transcript — caller chose not to route the turn."""


class JarvisService:
    """Run one turn of Jarvis using a configured Piper voice model."""

    def __init__(self, voice_model_path, *, runtime_source: str = "cli") -> None:
        self.voice_model_path = voice_model_path
        self._speaker = Speaker(voice_model_path)
        self._sdk_backend = sdk_backend.SDKBackend()
        self._active_turn_id: str | None = None
        self._runtime_owner = None
        try:
            self._runtime_owner = runtime_status.publisher(runtime_source)
        except Exception as exc:
            self._status_warning("publisher", exc)

    @staticmethod
    def _status_warning(operation: str, error: Exception) -> None:
        print(
            f"[jarvis] runtime status {operation} failed: {error}",
            file=sys.stderr,
        )

    def _publish_status(self, state: str, turn_id: str) -> None:
        if self._runtime_owner is None:
            return
        try:
            runtime_status.publish(self._runtime_owner, state, turn_id)
        except Exception as exc:
            self._status_warning("publish", exc)

    def _clear_status(self, turn_id: str) -> None:
        if self._runtime_owner is None:
            return
        try:
            runtime_status.clear(self._runtime_owner, turn_id)
        except Exception as exc:
            self._status_warning("clear", exc)

    def _fail_status(self, turn_id: str, code: str) -> None:
        if self._runtime_owner is None:
            return
        try:
            runtime_status.fail(self._runtime_owner, turn_id, code)
        except Exception as exc:
            self._status_warning("fail", exc)

    @staticmethod
    def _new_turn_id() -> str:
        return uuid4().hex

    def _begin_turn(self, turn_id: str) -> None:
        self._active_turn_id = turn_id

    def _finish_turn(self, turn_id: str) -> None:
        if self._active_turn_id == turn_id:
            self._active_turn_id = None

    def _cancel_turn(self, turn_id: str) -> None:
        if self._active_turn_id == turn_id:
            self._clear_status(turn_id)
            self._active_turn_id = None

    async def aanswer(
        self, text: str, *, stt_ms: float | None = None, speak: bool = True
    ) -> str:
        """Answer and speak one turn using the schedule provider or the SDK backend."""
        turn_id = self._new_turn_id()
        self._begin_turn(turn_id)
        return await self._answer_with_turn(
            text,
            stt_ms=stt_ms,
            speak=speak,
            turn_id=turn_id,
        )

    async def _answer_with_turn(
        self,
        text: str,
        *,
        stt_ms: float | None,
        speak: bool,
        turn_id: str,
        publish_processing: bool = True,
    ) -> str:
        started = time.monotonic()
        if publish_processing:
            self._publish_status("processing", turn_id)
        path = "sdk"
        try:
            is_schedule = router.route(text) == "schedule"
        except asyncio.CancelledError:
            self._cancel_turn(turn_id)
            raise
        except Exception as exc:
            self._fail_status(turn_id, "routing_failed")
            self._finish_turn(turn_id)
            self._record_failure(path, started, stt_ms, type(exc).__name__)
            raise

        path = "schedule" if is_schedule else "sdk"
        try:
            if is_schedule:
                response, dispatch_ms, dispatch_error = await self._dispatch_schedule(
                    speak=speak,
                    turn_id=turn_id,
                )
            else:
                response, dispatch_ms = await self._dispatch_sdk(
                    text,
                    speak=speak,
                    turn_id=turn_id,
                )
                dispatch_error = None
        except asyncio.CancelledError:
            self._cancel_turn(turn_id)
            raise
        except _TTSDispatchFailure as failure:
            self._finish_turn(turn_id)
            self._record_failure(
                path, started, stt_ms, type(failure.original).__name__
            )
            raise failure.original
        except _SDKDispatchFailure as failure:
            self._finish_turn(turn_id)
            self._record_failure(
                path, started, stt_ms, type(failure.original).__name__
            )
            raise failure.original
        except Exception as exc:
            self._fail_status(
                turn_id,
                "schedule_failed" if is_schedule else "sdk_failed",
            )
            self._finish_turn(turn_id)
            self._record_failure(path, started, stt_ms, type(exc).__name__)
            raise

        if dispatch_error is not None:
            if not speak:
                self._fail_status(turn_id, dispatch_error)
        elif not speak:
            self._clear_status(turn_id)

        self._finish_turn(turn_id)
        self._record_success(path, started, stt_ms, dispatch_ms)
        return response

    async def _dispatch_schedule(
        self, *, speak: bool, turn_id: str
    ) -> tuple[str, float, str | None]:
        dispatch_start = time.monotonic()
        response, dispatch_error = self._answer_schedule_with_code()
        if speak:
            await self._run_playback(
                self._schedule_playback(response, turn_id, dispatch_error),
                turn_id,
            )
        return (
            response,
            (time.monotonic() - dispatch_start) * 1000,
            dispatch_error,
        )

    async def _dispatch_sdk(
        self, text: str, *, speak: bool, turn_id: str
    ) -> tuple[str, float]:
        dispatch_start = time.monotonic()
        speaker = self._speaker if speak else None
        if speaker is None:
            response = await _speak_stream(
                self._sdk_backend.ask_stream(text),
                speaker,
            )
        else:
            response = await self._run_playback(
                self._stream_playback(text, turn_id),
                turn_id,
            )
        return response, (time.monotonic() - dispatch_start) * 1000

    async def _run_playback(self, worker, turn_id: str) -> object:
        playback_task = asyncio.create_task(worker)
        self._finish_turn(turn_id)
        try:
            await asyncio.wait((playback_task,))
        except asyncio.CancelledError:
            playback_task.add_done_callback(self._consume_playback_result)
            raise
        return playback_task.result()

    @staticmethod
    def _consume_playback_result(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _schedule_playback(
        self, response: str, turn_id: str, failure_code: str | None
    ) -> str:
        try:
            if response:
                self._publish_status("speaking", turn_id)
                await asyncio.to_thread(self._speaker.say, response)
        except asyncio.CancelledError:
            self._clear_status(turn_id)
            raise
        except Exception as exc:
            self._fail_status(turn_id, "tts_failed")
            raise _TTSDispatchFailure(exc) from exc
        if failure_code is None:
            self._clear_status(turn_id)
        else:
            self._fail_status(turn_id, failure_code)
        return response

    async def _stream_playback(self, text: str, turn_id: str) -> str:
        speech_error: Exception | None = None

        def mark_speaking() -> None:
            self._publish_status("speaking", turn_id)

        def mark_speech_error(error: Exception) -> None:
            nonlocal speech_error
            speech_error = error

        try:
            response = await _speak_stream(
                self._sdk_backend.ask_stream(text),
                self._speaker,
                on_speaking=mark_speaking,
                on_speech_error=mark_speech_error,
            )
        except asyncio.CancelledError:
            self._clear_status(turn_id)
            raise
        except Exception as error:
            if speech_error is not None:
                self._fail_status(turn_id, "tts_failed")
                raise _TTSDispatchFailure(speech_error) from speech_error
            self._fail_status(turn_id, "sdk_failed")
            raise _SDKDispatchFailure(error) from error
        self._clear_status(turn_id)
        return response

    def _record_success(self, path, started, stt_ms, dispatch_ms) -> None:
        entry = TelemetryEntry(
            path=path,
            duration_ms=(time.monotonic() - started) * 1000,
            stt_ms=stt_ms,
            dispatch_ms=dispatch_ms,
            tools_fired=self._sdk_backend.last_tools_fired if path == "sdk" else [],
            error=None,
        )
        self._append_safe(entry)

    def _record_failure(self, path, started, stt_ms, error_name) -> None:
        entry = TelemetryEntry(
            path=path,
            duration_ms=(time.monotonic() - started) * 1000,
            stt_ms=stt_ms,
            dispatch_ms=0.0,
            tools_fired=[],
            error=error_name,
        )
        self._append_safe(entry)

    @staticmethod
    def _append_safe(entry) -> None:
        try:
            store.append_entry(entry)
        except Exception as exc:
            print(f"[jarvis] telemetry write failed: {exc}", file=sys.stderr)

    @staticmethod
    def _format_event(event) -> str:
        if isinstance(event, dict):
            title = event.get("title") or event.get("summary") or event.get("name")
            if title is not None:
                return str(title)
        return str(event)

    def _answer_schedule(self) -> str:
        response, _ = self._answer_schedule_with_code()
        return response

    def _answer_schedule_with_code(self) -> tuple[str, str | None]:
        try:
            events = schedule_provider.get_upcoming_events()
        except PermissionError:
            return (
                "Calendar access isn't authorized yet. Grant Jarvis access in System "
                "Settings and try again.",
                "schedule_failed",
            )
        except RuntimeError as error:
            return f"I couldn't reach your calendar: {error}", "schedule_failed"
        if not events:
            return "You have no upcoming events.", None
        formatted_events = ", ".join(self._format_event(event) for event in events)
        return f"You have {len(events)} events: {formatted_events}", None

    async def arun_once(self) -> str:
        """Capture and transcribe one utterance, then speak the response."""
        turn_id = self._new_turn_id()
        self._begin_turn(turn_id)
        try:
            audio = capture.record_on_enter(
                on_started=lambda: self._publish_status("listening", turn_id)
            )
        except asyncio.CancelledError:
            self._cancel_turn(turn_id)
            raise
        except Exception as exc:
            self._fail_status(turn_id, "capture_failed")
            self._finish_turn(turn_id)
            raise
        stt_start = time.monotonic()
        self._publish_status("processing", turn_id)
        try:
            transcript = stt.transcribe(audio)
        except asyncio.CancelledError:
            self._cancel_turn(turn_id)
            raise
        except Exception:
            self._fail_status(turn_id, "stt_failed")
            self._finish_turn(turn_id)
            raise
        stt_ms = (time.monotonic() - stt_start) * 1000
        return await self._answer_with_turn(
            transcript,
            stt_ms=stt_ms,
            speak=True,
            turn_id=turn_id,
            publish_processing=False,
        )

    async def arun_audio(self, audio: np.ndarray) -> tuple[str, str]:
        """Transcribe an already-captured 16 kHz waveform and route it through the
        normal service pipeline without speaking aloud.

        Distinct from :meth:`arun_once`, the audio is supplied by the caller (for
        example the browser voice pipeline) instead of being captured here. STT runs
        on a background worker so the event loop stays free for unrelated work, the
        returned tuple is ``(transcript, response)`` and the reply is not spoken
        aloud. A cancelled turn clears only its own matching runtime status and never
        routes or dispatches the partial transcript.
        """
        turn_id = self._new_turn_id()
        self._begin_turn(turn_id)
        self._publish_status("processing", turn_id)
        try:
            stt_start = time.monotonic()
            transcript = await asyncio.to_thread(stt.transcribe, audio)
        except asyncio.CancelledError:
            self._cancel_turn(turn_id)
            raise
        except Exception:
            self._publish_status("stt_failed", turn_id)
            self._finish_turn(turn_id)
            raise
        stt_ms = (time.monotonic() - stt_start) * 1000
        if not transcript.strip():
            self._publish_status("stt_failed", turn_id)
            self._finish_turn(turn_id)
            raise NoSpeechDetectedError(transcript)
        response = await self._answer_with_turn(
            transcript,
            stt_ms=stt_ms,
            speak=False,
            turn_id=turn_id,
            publish_processing=False,
        )
        return transcript, response

    async def arun_text(self, text: str) -> str:
        """Speak a supplied text response without using audio capture or STT."""
        return await self.aanswer(text)

    async def aclose(self) -> None:
        """Release the SDK backend's connection, if one was ever opened."""
        active_turn_id = self._active_turn_id
        try:
            await self._sdk_backend.close()
        finally:
            if active_turn_id is not None and self._active_turn_id == active_turn_id:
                self._clear_status(active_turn_id)
                self._active_turn_id = None
