"""Application service connecting audio capture, transcription, and speech."""

import asyncio
from collections.abc import AsyncIterator

from jarvis.voice import capture, stt
from jarvis.orchestrator import router, sdk_backend
from jarvis.providers import schedule_provider
from jarvis.voice.tts import SentenceBuffer
from jarvis.voice.tts import Speaker


async def _speak_stream(chunks: AsyncIterator[str], speaker) -> str:
    """Speak streamed text sentence-by-sentence as it arrives; return the full text.

    A producer drains *chunks* and feeds a SentenceBuffer; the one consumer speaks
    completed sentences off the event loop via asyncio.to_thread, so a speaker.say()
    call in flight never blocks the producer from continuing to drain *chunks*. The
    returned text is the raw concatenation of every chunk received, tracked
    independently of how it was split into spoken sentences.
    """
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
            trailing = buffer.flush()
            if trailing:
                await queue.put(trailing)
        except BaseException as exc:
            producer_error.append(exc)
        finally:
            await queue.put(None)

    producer_task = asyncio.create_task(produce())
    try:
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            await asyncio.to_thread(speaker.say, sentence)
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

    async def aanswer(self, text: str) -> str:
        """Answer and speak one turn using the schedule provider or the SDK backend."""
        if router.route(text) == "schedule":
            response = self._answer_schedule()
            await asyncio.to_thread(self._speaker.say, response)
            return response
        return await _speak_stream(self._sdk_backend.ask_stream(text), self._speaker)

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
        transcript = stt.transcribe(audio)
        return await self.aanswer(transcript)

    async def arun_text(self, text: str) -> str:
        """Speak a supplied text response without using audio capture or STT."""
        return await self.aanswer(text)

    async def aclose(self) -> None:
        """Release the SDK backend's connection, if one was ever opened."""
        await self._sdk_backend.close()
