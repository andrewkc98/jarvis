"""Application service connecting audio capture, transcription, and speech."""

import asyncio
import sys
import time
from collections.abc import AsyncIterator

from jarvis.voice import capture, stt
from jarvis.orchestrator import router, sdk_backend
from jarvis.providers import schedule_provider
from jarvis.telemetry import store
from jarvis.telemetry.store import TelemetryEntry
from jarvis.voice.tts import SentenceBuffer
from jarvis.voice.tts import Speaker


async def _speak_stream(chunks: AsyncIterator[str], speaker) -> str:
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
        while True:
            sentence = asyncio.run_coroutine_threadsafe(queue.get(), loop).result()
            if sentence is None:
                return
            speaker.say(sentence)

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


class JarvisService:
    """Run one turn of Jarvis using a configured Piper voice model."""

    def __init__(self, voice_model_path) -> None:
        self.voice_model_path = voice_model_path
        self._speaker = Speaker(voice_model_path)
        self._sdk_backend = sdk_backend.SDKBackend()

    async def aanswer(
        self, text: str, *, stt_ms: float | None = None, speak: bool = True
    ) -> str:
        """Answer and speak one turn using the schedule provider or the SDK backend."""
        started = time.monotonic()
        is_schedule = router.route(text) == "schedule"
        path = "schedule" if is_schedule else "sdk"
        try:
            if is_schedule:
                response, dispatch_ms = await self._dispatch_schedule(speak=speak)
            else:
                response, dispatch_ms = await self._dispatch_sdk(text, speak=speak)
        except Exception as exc:
            self._record_failure(path, started, stt_ms, type(exc).__name__)
            raise

        self._record_success(path, started, stt_ms, dispatch_ms)
        return response

    async def _dispatch_schedule(self, *, speak: bool) -> tuple[str, float]:
        dispatch_start = time.monotonic()
        response = self._answer_schedule()
        if speak:
            await asyncio.to_thread(self._speaker.say, response)
        return response, (time.monotonic() - dispatch_start) * 1000

    async def _dispatch_sdk(self, text: str, *, speak: bool) -> tuple[str, float]:
        dispatch_start = time.monotonic()
        speaker = self._speaker if speak else None
        response = await _speak_stream(self._sdk_backend.ask_stream(text), speaker)
        return response, (time.monotonic() - dispatch_start) * 1000

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
        try:
            events = schedule_provider.get_upcoming_events()
        except PermissionError:
            return (
                "Calendar access isn't authorized yet. Grant Jarvis access in System "
                "Settings and try again."
            )
        except RuntimeError as error:
            return f"I couldn't reach your calendar: {error}"
        if not events:
            return "You have no upcoming events."
        formatted_events = ", ".join(self._format_event(event) for event in events)
        return f"You have {len(events)} events: {formatted_events}"

    async def arun_once(self) -> str:
        """Capture and transcribe one utterance, then speak the response."""
        audio = capture.record_on_enter()
        stt_start = time.monotonic()
        transcript = stt.transcribe(audio)
        stt_ms = (time.monotonic() - stt_start) * 1000
        return await self.aanswer(transcript, stt_ms=stt_ms)

    async def arun_text(self, text: str) -> str:
        """Speak a supplied text response without using audio capture or STT."""
        return await self.aanswer(text)

    async def aclose(self) -> None:
        """Release the SDK backend's connection, if one was ever opened."""
        await self._sdk_backend.close()
