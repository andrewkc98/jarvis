"""FastAPI app exposing localhost-only read endpoints for the HUD."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import shutil
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from jarvis import config
from jarvis.approvals import store as approvals
from jarvis.orchestrator.service import JarvisService, NoSpeechDetectedError
from jarvis.providers import schedule_provider, vault_provider
from jarvis.runtime import status as runtime_status
from jarvis.telemetry import store
from jarvis.tools import vitals
from jarvis.voice import media as voice_media
from jarvis.voice.tts import Speaker

DAILY_NOTE_EXCERPT_LIMIT = 5000


def _truncated_excerpt(content: str) -> str:
    if len(content) <= DAILY_NOTE_EXCERPT_LIMIT:
        return content
    return content[:DAILY_NOTE_EXCERPT_LIMIT] + "..."


_service: JarvisService | None = None
_command_turn_lock = asyncio.Lock()
_speaker: Speaker | None = None
_speech_lock = asyncio.Lock()
MAX_SPEECH_TEXT_CODE_POINTS = 10_000
_TURN_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


_VOICE_TURN_TIMEOUT_SECONDS = 120


@dataclasses.dataclass
class _VoiceTurn:
    """The single active browser voice turn: its exact ID and dedicated worker."""

    turn_id: str
    task: asyncio.Task | None = None
    lock: asyncio.Lock | None = None
    cancel_requested: bool = False
    cancel_delivered: bool = False
    cleanup_done: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    finalizer: asyncio.Task | None = None


_active_voice_turn: _VoiceTurn | None = None


class _UploadTooLarge(Exception):
    pass


def _canonical_turn_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36 or not value.isascii():
        return None
    if _TURN_ID_PATTERN.fullmatch(value) is None:
        return None
    try:
        if str(uuid.UUID(value)) != value:
            return None
    except ValueError:
        return None
    return value


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code})


async def _read_bounded_body(request: Request) -> bytes:
    limit = voice_media.MAX_UPLOAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        declared = declared.strip()
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            raise _UploadTooLarge()
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > limit:
            raise _UploadTooLarge()
        buffer.extend(chunk)
    return bytes(buffer)


async def _run_voice_turn(request: Request, content_type: str) -> tuple[str, str]:
    body = await _read_bounded_body(request)
    audio = await asyncio.to_thread(voice_media.decode_media, body, content_type)
    del body
    try:
        service = _get_service()
    except Exception:
        try:
            runtime_status.record_error("api", "service_unavailable")
        except Exception:
            pass
        raise
    return await service.arun_audio(audio)


async def _complete_despite_cancel(awaitable) -> None:
    """Run ``awaitable`` to completion even if the caller is cancelled meanwhile,
    then re-raise that cancellation."""
    inner = asyncio.ensure_future(awaitable)
    cancelled = False
    while not inner.done():
        try:
            await asyncio.shield(inner)
        except asyncio.CancelledError:
            if inner.done() and not inner.cancelled():
                break
            cancelled = True
        except Exception:
            break
    if not inner.cancelled():
        inner.exception()
    if cancelled:
        raise asyncio.CancelledError()


async def _discard_service() -> None:
    """Close and drop the API singleton so an interrupted SDK cycle is never reused."""
    global _service
    service = _service
    try:
        if service is not None:
            try:
                await asyncio.shield(service.aclose())
            except Exception:
                pass
    finally:
        if _service is service:
            _service = None


async def _finalize_voice_turn(record: _VoiceTurn) -> None:
    """Sole owner of post-worker cleanup: await the worker, discard an interrupted
    service, clear the matching registry record, signal completion, release the lock."""
    global _active_voice_turn
    try:
        await asyncio.wait({record.task})
        if record.cancel_delivered:
            await _discard_service()
    finally:
        active = _active_voice_turn
        if (
            active is not None
            and active.turn_id == record.turn_id
            and active.task is record.task
        ):
            _active_voice_turn = None
        record.cleanup_done.set()
        if record.lock is not None and record.lock.locked():
            record.lock.release()


async def _cancel_voice_turn(record: _VoiceTurn) -> None:
    """Single-cancel coordinator: deliver ``Task.cancel()`` at most once, then wait
    for cleanup to complete."""
    if not record.cancel_requested:
        record.cancel_requested = True
        if not record.task.done():
            record.cancel_delivered = True
            record.task.cancel()
    await record.cleanup_done.wait()


def _voice_turn_error(exc: BaseException) -> JSONResponse:
    if isinstance(exc, _UploadTooLarge) or isinstance(exc, voice_media.MediaTooLargeError):
        return _error(413, "audio_too_large")
    if isinstance(exc, voice_media.UnsupportedMediaTypeError):
        return _error(415, "unsupported_audio_type")
    if isinstance(exc, voice_media.EmptyAudioError):
        return _error(422, "empty_audio")
    if isinstance(exc, voice_media.NoDecoderError):
        return _error(503, "decoder_unavailable")
    if isinstance(exc, voice_media.DecodeTimeoutError):
        return _error(504, "audio_decode_timeout")
    if isinstance(exc, voice_media.AudioDurationOverflowError):
        return _error(413, "audio_too_long")
    if isinstance(exc, voice_media.MediaDecodeError):
        return _error(422, "audio_decode_failed")
    if isinstance(exc, NoSpeechDetectedError):
        return _error(422, "no_speech_detected")
    return _error(502, "voice_turn_failed")


_RUNTIME_STATES = frozenset({"listening", "processing", "speaking", "error"})


def _idle_runtime_status() -> dict[str, object]:
    return {"state": "idle", "source": None, "updated_at": None, "error_code": None}


def _safe_runtime_status() -> dict[str, object]:
    try:
        raw = runtime_status.snapshot()
        if not isinstance(raw, dict):
            return _idle_runtime_status()
        state = raw.get("state")
        if state == "idle":
            return _idle_runtime_status()
        if state not in _RUNTIME_STATES:
            return _idle_runtime_status()
        source = raw.get("source")
        updated_at = raw.get("updated_at")
        if source not in runtime_status.SOURCES or not isinstance(updated_at, str):
            return _idle_runtime_status()
        runtime_status._parse_aware_timestamp(updated_at, "updated_at")
        if state == "error":
            error_code = raw.get("error_code")
            if error_code not in runtime_status.ERROR_CODES:
                return _idle_runtime_status()
            return {
                "state": "error",
                "source": source,
                "updated_at": updated_at,
                "error_code": error_code,
            }
        if raw.get("error_code") is not None:
            return _idle_runtime_status()
        return {
            "state": state,
            "source": source,
            "updated_at": updated_at,
            "error_code": None,
        }
    except Exception:
        return _idle_runtime_status()


def _voice_capabilities() -> dict[str, object]:
    mode = config.VOICE_INPUT_MODE
    if mode == "terminal":
        reason = "terminal_voice_active"
    elif mode == "off":
        reason = "voice_disabled"
    elif not config.VOICE_MODEL_PATH:
        reason = "voice_model_not_configured"
    elif shutil.which("ffmpeg") is None:
        reason = "decoder_unavailable"
    else:
        reason = None
    return {
        "mode": mode,
        "available": reason is None,
        "reason": reason,
        "max_duration_seconds": voice_media.MAX_DURATION_SECONDS,
        "max_upload_bytes": voice_media.MAX_UPLOAD_BYTES,
        "accepted_media_types": list(voice_media.ACCEPTED_MEDIA_TYPES),
    }


def _get_speaker() -> Speaker:
    global _speaker
    if _speaker is None:
        _speaker = Speaker(config.VOICE_MODEL_PATH)
    return _speaker


def _get_service() -> JarvisService:
    global _service
    if _service is None:
        if not config.VOICE_MODEL_PATH:
            raise RuntimeError("JARVIS_VOICE_MODEL is not set")
        _service = JarvisService(
            voice_model_path=config.VOICE_MODEL_PATH, runtime_source="api"
        )
    return _service


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    record = _active_voice_turn
    if record is not None:
        await _complete_despite_cancel(_cancel_voice_turn(record))
    async with _command_turn_lock:
        if _service is not None:
            await _service.aclose()
    global _speaker
    async with _speech_lock:
        _speaker = None


def _register_routes(app: FastAPI) -> None:
    @app.get("/vitals")
    def get_vitals() -> dict:
        return vitals.get_vitals()

    @app.get("/schedule")
    def get_schedule():
        try:
            events = schedule_provider.get_upcoming_events()
        except PermissionError:
            return JSONResponse(
                status_code=503, content={"error": "calendar_permission_required"}
            )
        except Exception:
            return JSONResponse(
                status_code=502, content={"error": "calendar_unavailable"}
            )
        return {
            "events": [
                {
                    "summary": event["summary"],
                    "start": event["start"],
                    "end": event["end"],
                    "all_day": event["all_day"],
                }
                for event in events
            ]
        }

    @app.get("/vault-summary")
    def get_vault_summary():
        try:
            content, open_task_count = (
                vault_provider.get_daily_note_and_task_count()
            )
        except Exception:
            return JSONResponse(
                status_code=502, content={"error": "obsidian_unavailable"}
            )
        return {
            "daily_note_excerpt": _truncated_excerpt(content),
            "open_task_count": open_task_count,
        }

    @app.get("/telemetry")
    def get_telemetry(limit: int = Query(default=20, ge=1, le=200)) -> dict:
        return {"entries": store.read_recent(limit)}

    @app.get("/runtime-status")
    def get_runtime_status() -> dict:
        return _safe_runtime_status()

    @app.get("/pending-approval")
    def get_pending_approval() -> dict:
        record = approvals.peek()
        if record is None or record.decision is not None:
            return {"pending": None}
        return {"pending": dataclasses.asdict(record)}

    def _decide(body: dict, decision: str) -> JSONResponse | dict:
        pending_id = body.get("id")
        if not pending_id or not approvals.decide(
            pending_id, decision, decided_by="hud"
        ):
            return JSONResponse(
                status_code=409, content={"error": "expired_or_mismatched"}
            )
        return {"ok": True}

    @app.post("/allow")
    def post_allow(body: dict):
        return _decide(body, "allow")

    @app.post("/deny")
    def post_deny(body: dict):
        return _decide(body, "deny")

    @app.get("/voice-capabilities")
    def get_voice_capabilities() -> dict:
        return _voice_capabilities()

    @app.post("/command")
    async def post_command(body: dict):
        text = body.get("text")
        if text is None or text == "":
            return JSONResponse(
                status_code=422, content={"error": "text is required"}
            )
        if not isinstance(text, str):
            return JSONResponse(status_code=422, content={"error": "invalid request"})
        speak = body.get("speak", True)
        if not isinstance(speak, bool):
            return JSONResponse(status_code=422, content={"error": "invalid request"})
        if not config.VOICE_MODEL_PATH:
            return JSONResponse(
                status_code=503, content={"error": "voice_model_not_configured"}
            )
        async with _command_turn_lock:
            try:
                service = _get_service()
            except Exception:
                try:
                    runtime_status.record_error("api", "service_unavailable")
                except Exception:
                    pass
                return JSONResponse(status_code=502, content={"error": "command_failed"})
            try:
                response = await service.aanswer(text, speak=speak)
            except Exception:
                return JSONResponse(status_code=502, content={"error": "command_failed"})
        return {"response": response}

    @app.post("/voice-turn")
    async def post_voice_turn(request: Request):
        global _active_voice_turn
        if config.VOICE_INPUT_MODE != "browser":
            return _error(503, "browser_voice_unavailable")
        turn_id = _canonical_turn_id(request.query_params.get("turn_id"))
        if turn_id is None:
            return _error(422, "invalid_turn_id")
        if not config.VOICE_MODEL_PATH:
            return _error(503, "voice_model_not_configured")
        lock = _command_turn_lock
        if lock.locked() or _active_voice_turn is not None:
            return _error(409, "turn_in_progress")
        await lock.acquire()
        record = _VoiceTurn(turn_id, lock=lock)
        try:
            content_type = request.headers.get("content-type", "")
            record.task = asyncio.create_task(_run_voice_turn(request, content_type))
            _active_voice_turn = record
            record.finalizer = asyncio.create_task(_finalize_voice_turn(record))
        except BaseException:
            if _active_voice_turn is record:
                _active_voice_turn = None
            lock.release()
            raise
        try:
            done, _pending = await asyncio.wait(
                {record.task}, timeout=_VOICE_TURN_TIMEOUT_SECONDS
            )
            if not done:
                await _complete_despite_cancel(_cancel_voice_turn(record))
                return _error(504, "voice_turn_timeout")
            await _complete_despite_cancel(record.cleanup_done.wait())
            if record.task.cancelled():
                return _error(409, "voice_turn_cancelled")
            try:
                transcript, response = record.task.result()
            except Exception as exc:
                return _voice_turn_error(exc)
            return JSONResponse(
                content={
                    "turn_id": turn_id,
                    "transcript": transcript,
                    "response": response,
                },
                headers={"Cache-Control": "no-store"},
            )
        except asyncio.CancelledError:
            await _complete_despite_cancel(_cancel_voice_turn(record))
            raise

    @app.post("/voice-speech")
    async def post_voice_speech(request: Request):
        if config.VOICE_INPUT_MODE != "browser":
            return _error(503, "browser_voice_unavailable")
        try:
            body = await request.json()
        except Exception:
            return _error(422, "invalid_speech_text")
        if not isinstance(body, dict) or set(body) != {"text"}:
            return _error(422, "invalid_speech_text")
        text = body["text"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > MAX_SPEECH_TEXT_CODE_POINTS
        ):
            return _error(422, "invalid_speech_text")
        if not config.VOICE_MODEL_PATH:
            return _error(503, "voice_model_not_configured")
        lock = _speech_lock
        if lock.locked():
            return _error(409, "speech_busy")
        await lock.acquire()
        try:
            try:
                speaker = _get_speaker()
            except Exception:
                return _error(502, "speech_failed")
            job = asyncio.ensure_future(asyncio.to_thread(speaker.synthesize_wav, text))
            # Keep the lock until the native worker really finishes, even if the
            # client disconnects, so two syntheses never share one Piper voice.
            await _complete_despite_cancel(job)
            try:
                wav = job.result()
            except Exception:
                return _error(502, "speech_failed")
            return Response(
                content=wav,
                media_type="audio/wav",
                headers={"Cache-Control": "no-store"},
            )
        finally:
            lock.release()

    @app.post("/voice-cancel")
    async def post_voice_cancel(request: Request):
        no_match = _error(409, "no_matching_turn")
        try:
            body = await request.json()
        except Exception:
            return no_match
        if not isinstance(body, dict) or set(body) != {"turn_id"}:
            return no_match
        turn_id = _canonical_turn_id(body["turn_id"])
        record = _active_voice_turn
        if turn_id is None or record is None or record.turn_id != turn_id:
            return no_match
        if record.task.done() and not record.cancel_requested:
            return no_match
        await _complete_despite_cancel(_cancel_voice_turn(record))
        return {"cancelled": True}


def build_app() -> FastAPI:
    fastapi_app = FastAPI(lifespan=_lifespan)
    if config.HUD_ORIGIN:
        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=[config.HUD_ORIGIN],
            allow_methods=["GET", "POST"],
        )
    _register_routes(fastapi_app)
    return fastapi_app


app = build_app()
