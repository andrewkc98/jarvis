"""FastAPI app exposing localhost-only read endpoints for the HUD."""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jarvis import config
from jarvis.approvals import store as approvals
from jarvis.orchestrator.service import JarvisService
from jarvis.providers import schedule_provider, vault_provider
from jarvis.telemetry import store
from jarvis.tools import vitals

DAILY_NOTE_EXCERPT_LIMIT = 5000


def _truncated_excerpt(content: str) -> str:
    if len(content) <= DAILY_NOTE_EXCERPT_LIMIT:
        return content
    return content[:DAILY_NOTE_EXCERPT_LIMIT] + "..."


_service: JarvisService | None = None


def _get_service() -> JarvisService:
    global _service
    if _service is None:
        if not config.VOICE_MODEL_PATH:
            raise RuntimeError("JARVIS_VOICE_MODEL is not set")
        _service = JarvisService(voice_model_path=config.VOICE_MODEL_PATH)
    return _service


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    if _service is not None:
        await _service.aclose()


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

    @app.post("/command")
    async def post_command(body: dict):
        text = body.get("text")
        if not text:
            return JSONResponse(
                status_code=422, content={"error": "text is required"}
            )
        if not config.VOICE_MODEL_PATH:
            return JSONResponse(
                status_code=503, content={"error": "voice_model_not_configured"}
            )
        speak = bool(body.get("speak", True))
        try:
            service = _get_service()
            response = await service.aanswer(text, speak=speak)
        except Exception:
            return JSONResponse(status_code=502, content={"error": "command_failed"})
        return {"response": response}


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
