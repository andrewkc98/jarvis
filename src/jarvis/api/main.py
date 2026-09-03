"""FastAPI app exposing localhost-only read endpoints for the HUD."""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jarvis import config
from jarvis.providers import schedule_provider, vault_provider
from jarvis.telemetry import store
from jarvis.tools import vitals

DAILY_NOTE_EXCERPT_LIMIT = 5000


def _truncated_excerpt(content: str) -> str:
    if len(content) <= DAILY_NOTE_EXCERPT_LIMIT:
        return content
    return content[:DAILY_NOTE_EXCERPT_LIMIT] + "..."


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


def build_app() -> FastAPI:
    fastapi_app = FastAPI()
    if config.HUD_ORIGIN:
        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=[config.HUD_ORIGIN],
        )
    _register_routes(fastapi_app)
    return fastapi_app


app = build_app()
