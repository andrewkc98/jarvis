import asyncio
import threading
import uuid

import httpx
import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from jarvis import config, telemetry
from jarvis.api import launcher, main as api_main
from jarvis.api.main import app as the_app, build_app
from jarvis.approvals.store import PendingApproval
from jarvis.providers import schedule_provider, vault_provider
from jarvis.telemetry import store
from jarvis.tools import vitals
from jarvis.voice import media as voice_media


@pytest.fixture
def monkeypatched_client(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", None, raising=True)
    monkeypatch.setattr(
        schedule_provider,
        "get_upcoming_events",
        lambda: [
            {
                "summary": "Standup",
                "start": "2026-09-03T09:00:00Z",
                "end": "2026-09-03T09:15:00Z",
                "all_day": False,
            },
            {
                "summary": "Company Holiday",
                "start": "2026-09-04T00:00:00Z",
                "end": "2026-09-05T00:00:00Z",
                "all_day": True,
            },
        ],
    )
    monkeypatch.setattr(
        vault_provider,
        "get_daily_note_and_task_count",
        lambda: ("short note", 3),
    )
    monkeypatch.setattr(
        store,
        "read_recent",
        lambda limit: [{"path": "sdk", "tools_fired": []}],
    )
    monkeypatch.setattr(
        vitals,
        "get_vitals",
        lambda: {
            "cpu_percent": 1.0,
            "ram_percent": 2.0,
            "disk_percent": 3.0,
            "gpu": "unavailable",
        },
    )
    return TestClient(build_app())


def test_vitals_returns_mocked_dict(monkeypatched_client):
    client = monkeypatched_client
    response = client.get("/vitals")
    assert response.status_code == 200
    assert response.json() == {
        "cpu_percent": 1.0,
        "ram_percent": 2.0,
        "disk_percent": 3.0,
        "gpu": "unavailable",
    }


def test_schedule_success(monkeypatched_client):
    client = monkeypatched_client
    response = client.get("/schedule")
    assert response.status_code == 200
    assert response.json() == {
        "events": [
            {
                "summary": "Standup",
                "start": "2026-09-03T09:00:00Z",
                "end": "2026-09-03T09:15:00Z",
                "all_day": False,
            },
            {
                "summary": "Company Holiday",
                "start": "2026-09-04T00:00:00Z",
                "end": "2026-09-05T00:00:00Z",
                "all_day": True,
            },
        ]
    }


def test_schedule_permission_denied(monkeypatch):
    monkeypatch.setattr(
        schedule_provider,
        "get_upcoming_events",
        lambda: (_ for _ in ()).throw(PermissionError("denied")),
    )
    client = TestClient(build_app())
    response = client.get("/schedule")
    assert response.status_code == 503
    assert response.json() == {"error": "calendar_permission_required"}


def test_schedule_unavailable(monkeypatch):
    monkeypatch.setattr(
        schedule_provider,
        "get_upcoming_events",
        lambda: (_ for _ in ()).throw(ConnectionError("boom secret detail")),
    )
    client = TestClient(build_app())
    response = client.get("/schedule")
    assert response.status_code == 502
    assert response.json() == {"error": "calendar_unavailable"}


def test_vault_summary_success_and_truncation(monkeypatch):
    long_note = "x" * 5001
    monkeypatch.setattr(
        vault_provider,
        "get_daily_note_and_task_count",
        lambda: (long_note, 7),
    )
    client = TestClient(build_app())
    response = client.get("/vault-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["open_task_count"] == 7
    assert body["daily_note_excerpt"].endswith("...")
    assert len(body["daily_note_excerpt"]) == 5003
    assert body["daily_note_excerpt"][:5000] == long_note[:5000]


def test_vault_summary_unavailable(monkeypatch):
    monkeypatch.setattr(
        vault_provider,
        "get_daily_note_and_task_count",
        lambda: (_ for _ in ()).throw(RuntimeError("obsidian down")),
    )
    client = TestClient(build_app())
    response = client.get("/vault-summary")
    assert response.status_code == 502
    assert response.json() == {"error": "obsidian_unavailable"}


def test_telemetry_success(monkeypatched_client):
    client = monkeypatched_client
    response = client.get("/telemetry")
    assert response.status_code == 200
    assert response.json() == {"entries": [{"path": "sdk", "tools_fired": []}]}


def test_telemetry_empty_store(monkeypatch):
    monkeypatch.setattr(store, "read_recent", lambda limit: [])
    client = TestClient(build_app())
    response = client.get("/telemetry")
    assert response.status_code == 200
    assert response.json() == {"entries": []}


def test_telemetry_limit_validation(monkeypatched_client):
    client = monkeypatched_client
    assert client.get("/telemetry", params={"limit": 0}).status_code == 422
    assert client.get("/telemetry", params={"limit": 201}).status_code == 422


def test_runtime_status_returns_exact_safe_snapshot(monkeypatch, reset_service_singleton):
    snapshot = {
        "state": "speaking",
        "source": "api",
        "updated_at": "2026-09-03T12:00:00+00:00",
        "error_code": None,
        "instances": {"private": "must not be exposed"},
    }
    monkeypatch.setattr(api_main.runtime_status, "snapshot", lambda: snapshot)
    response = TestClient(build_app()).get("/runtime-status")
    assert response.status_code == 200
    assert response.json() == {
        "state": "speaking",
        "source": "api",
        "updated_at": "2026-09-03T12:00:00+00:00",
        "error_code": None,
    }


def test_runtime_status_error_shape_is_safe(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(
        api_main.runtime_status,
        "snapshot",
        lambda: {
            "state": "error",
            "source": "cli",
            "updated_at": "2026-09-03T12:00:00+00:00",
            "error_code": "sdk_failed",
        },
    )
    response = TestClient(build_app()).get("/runtime-status")
    assert response.status_code == 200
    assert response.json() == {
        "state": "error",
        "source": "cli",
        "updated_at": "2026-09-03T12:00:00+00:00",
        "error_code": "sdk_failed",
    }


def test_runtime_status_store_failure_returns_canonical_idle(
    monkeypatch, reset_service_singleton
):
    def fail_snapshot():
        raise RuntimeError("sensitive backend detail")

    monkeypatch.setattr(api_main.runtime_status, "snapshot", fail_snapshot)
    response = TestClient(build_app()).get("/runtime-status")
    assert response.status_code == 200
    assert response.json() == {
        "state": "idle",
        "source": None,
        "updated_at": None,
        "error_code": None,
    }


def test_cors_header_absent_when_hud_origin_unset(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", None, raising=True)
    client = TestClient(build_app())
    response = client.get("/vitals", headers={"Origin": "https://hud.local"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_header_present_when_hud_origin_matches(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    client = TestClient(build_app())
    response = client.get("/vitals", headers={"Origin": "https://hud.local"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://hud.local"


def test_cors_preflight_when_hud_origin_matches(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    client = TestClient(build_app())
    response = client.options(
        "/vitals",
        headers={
            "Origin": "https://hud.local",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://hud.local"
    assert "GET" in response.headers.get("access-control-allow-methods", "")


def test_cors_preflight_allows_post_for_command_and_approval_routes(monkeypatch):
    """The HUD's approve/deny buttons and command box all POST with a JSON body
    (a non-safelisted Content-Type), which triggers a real browser preflight —
    unlike the GET-only routes Phase 4 shipped, these must actually allow POST."""
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    client = TestClient(build_app())
    for path in ("/command", "/allow", "/deny"):
        response = client.options(
            path,
            headers={
                "Origin": "https://hud.local",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert response.status_code == 200, f"{path}: {response.text}"
        assert "POST" in response.headers.get("access-control-allow-methods", "")


def test_voice_capabilities_response_shape_exact_keys(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "browser", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    client = TestClient(build_app())
    response = client.get("/voice-capabilities")
    assert response.status_code == 200
    assert set(response.json()) == {
        "mode",
        "available",
        "reason",
        "max_duration_seconds",
        "max_upload_bytes",
        "accepted_media_types",
    }


def test_voice_capabilities_terminal_mode(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "terminal", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    client = TestClient(build_app())
    assert client.get("/voice-capabilities").json() == {
        "mode": "terminal",
        "available": False,
        "reason": "terminal_voice_active",
        "max_duration_seconds": voice_media.MAX_DURATION_SECONDS,
        "max_upload_bytes": voice_media.MAX_UPLOAD_BYTES,
        "accepted_media_types": list(voice_media.ACCEPTED_MEDIA_TYPES),
    }


def test_voice_capabilities_off_mode(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "off", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    client = TestClient(build_app())
    assert client.get("/voice-capabilities").json() == {
        "mode": "off",
        "available": False,
        "reason": "voice_disabled",
        "max_duration_seconds": voice_media.MAX_DURATION_SECONDS,
        "max_upload_bytes": voice_media.MAX_UPLOAD_BYTES,
        "accepted_media_types": list(voice_media.ACCEPTED_MEDIA_TYPES),
    }


def test_voice_capabilities_terminal_reason_wins_over_missing_model(
    monkeypatch, reset_service_singleton
):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "terminal", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", None, raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: None)
    client = TestClient(build_app())
    body = client.get("/voice-capabilities").json()
    assert body["available"] is False
    assert body["reason"] == "terminal_voice_active"


def test_voice_capabilities_browser_without_model(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "browser", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", None, raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    client = TestClient(build_app())
    assert client.get("/voice-capabilities").json() == {
        "mode": "browser",
        "available": False,
        "reason": "voice_model_not_configured",
        "max_duration_seconds": voice_media.MAX_DURATION_SECONDS,
        "max_upload_bytes": voice_media.MAX_UPLOAD_BYTES,
        "accepted_media_types": list(voice_media.ACCEPTED_MEDIA_TYPES),
    }


def test_voice_capabilities_browser_model_missing_decoder(
    monkeypatch, reset_service_singleton
):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "browser", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: None)
    client = TestClient(build_app())
    assert client.get("/voice-capabilities").json() == {
        "mode": "browser",
        "available": False,
        "reason": "decoder_unavailable",
        "max_duration_seconds": voice_media.MAX_DURATION_SECONDS,
        "max_upload_bytes": voice_media.MAX_UPLOAD_BYTES,
        "accepted_media_types": list(voice_media.ACCEPTED_MEDIA_TYPES),
    }


def test_voice_capabilities_browser_available(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "browser", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(api_main.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    client = TestClient(build_app())
    assert client.get("/voice-capabilities").json() == {
        "mode": "browser",
        "available": True,
        "reason": None,
        "max_duration_seconds": 30,
        "max_upload_bytes": 8388608,
        "accepted_media_types": ["audio/webm", "audio/ogg", "audio/mp4"],
    }


@pytest.fixture
def voice_probe(monkeypatch, reset_service_singleton):
    """Track decoder discovery and prove the endpoint stays side-effect free."""
    state = {
        "which_called_with": [],
        "service_constructed": False,
    }

    def fake_which(name):
        state["which_called_with"].append(name)
        return None

    class FakeService:
        def __init__(self, *args, **kwargs):
            state["service_constructed"] = True

        async def aclose(self):
            pass

    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "off", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(api_main.shutil, "which", fake_which)
    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    monkeypatch.setattr(voice_media, "decode_media", lambda raw, content_type: None)
    return state


def test_voice_capabilities_does_not_construct_service_or_decode(
    monkeypatch, voice_probe
):
    assert api_main._service is None
    client = TestClient(build_app())
    response = client.get("/voice-capabilities")
    assert response.status_code == 200
    assert response.json()["reason"] == "voice_disabled"
    assert voice_probe["which_called_with"] == []
    assert voice_probe["service_constructed"] is False
    assert api_main._service is None


def test_voice_capabilities_exposes_no_paths(monkeypatch, voice_probe):
    client = TestClient(build_app())
    body = client.get("/voice-capabilities").json()
    text = sorted(str(value) for value in body.values())
    for value in text:
        assert "voice.onnx" not in value
        assert "/tmp" not in value
        assert "ffmpeg" not in value


def test_voice_capabilities_survives_probe_failure(monkeypatch, voice_probe):
    def broken_which(name):
        raise OSError("boom")

    monkeypatch.setattr(api_main.shutil, "which", broken_which)
    client = TestClient(build_app())
    body = client.get("/voice-capabilities").json()
    assert body["mode"] == "off"
    assert body["available"] is False
    assert body["reason"] == "voice_disabled"


def test_cors_preflight_allows_voice_turn_upload(monkeypatch):
    """A browser-mode page records with MediaRecorder (audio/webm;codecs=opus)
    and must be able to preflight POST /voice-turn against the exact HUD origin
    using only the safelisted Content-Type header name."""
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    client = TestClient(build_app())
    response = client.options(
        "/voice-turn",
        headers={
            "Origin": "https://hud.local",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin") == "https://hud.local"
    assert "POST" in response.headers.get("access-control-allow-methods", "")
    allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
    # The installed Starlette safelists Content-Type for preflight; the
    # allowed set must be exactly that safelist — no wildcard, no custom header.
    assert set(allowed_headers.split(", ")) == {
        "accept",
        "accept-language",
        "content-language",
        "content-type",
    }


def test_cors_preflight_rejects_unconfigured_origin_for_voice_turn(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    client = TestClient(build_app())
    response = client.options(
        "/voice-turn",
        headers={
            "Origin": "https://other.local",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    assert "origin" in response.text


def test_cors_preflight_rejects_unallowed_method(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    client = TestClient(build_app())
    response = client.options(
        "/voice-turn",
        headers={
            "Origin": "https://hud.local",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert response.status_code == 400
    allowed_methods = response.headers.get("access-control-allow-methods", "")
    assert "DELETE" not in [
        method.strip() for method in allowed_methods.split(",")
    ]
    assert "method" in response.text


def test_cors_middleware_keeps_exact_origin_and_get_post_only(monkeypatch):
    """The capabilities endpoint is a same-page GET, so the existing
    middleware must already serve it: exact origin, GET/POST only, no
    credentials, no wildcard, and only the safelisted Content-Type request
    header allowed."""
    monkeypatch.setattr(config, "HUD_ORIGIN", "https://hud.local", raising=True)
    fastapi_app = build_app()
    middleware = fastapi_app.user_middleware[0]
    assert middleware.cls is CORSMiddleware
    assert middleware.kwargs == {
        "allow_origins": ["https://hud.local"],
        "allow_methods": ["GET", "POST"],
    }
    instance = CORSMiddleware(fastapi_app.router, **middleware.kwargs)
    assert instance.allow_origins == ["https://hud.local"]
    assert instance.allow_all_origins is False
    assert instance.allow_methods == ["GET", "POST"]
    assert instance.allow_credentials is False
    assert instance.allow_headers == sorted(
        {"accept", "accept-language", "content-language", "content-type"}
    )


def test_launcher_main_calls_uvicorn(monkeypatch):
    called = {}

    def fake_run(app, host=None, port=None, **kwargs):
        called["app"] = app
        called["host"] = host
        called["port"] = port

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    launcher.main()
    assert called["host"] == "127.0.0.1"
    assert called["port"] == config.API_PORT
    from jarvis.api.main import app as the_app

    assert called["app"] is the_app


def _pending_record(decision=None):
    return PendingApproval(
        id="abc123",
        tool_name="obsidian_write",
        description="Write a note",
        requested_at="2026-09-03T10:00:00Z",
        expires_at="2026-09-03T10:30:00Z",
        decision=decision,
        decided_by="hud" if decision is not None else None,
    )


@pytest.fixture
def reset_service_singleton(monkeypatch):
    monkeypatch.setattr(api_main, "_service", None, raising=True)
    monkeypatch.setattr(api_main, "_command_turn_lock", asyncio.Lock(), raising=True)
    monkeypatch.setattr(api_main, "_active_voice_turn", None, raising=True)
    monkeypatch.setattr(api_main, "_speaker", None, raising=True)
    monkeypatch.setattr(api_main, "_speech_lock", asyncio.Lock(), raising=True)
    return monkeypatch


def test_pending_approval_none(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(api_main.approvals, "peek", lambda: None)
    client = TestClient(build_app())
    response = client.get("/pending-approval")
    assert response.status_code == 200
    assert response.json() == {"pending": None}


def test_pending_approval_decided_but_not_cleared(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(
        api_main.approvals, "peek", lambda: _pending_record(decision="allow")
    )
    client = TestClient(build_app())
    response = client.get("/pending-approval")
    assert response.status_code == 200
    assert response.json() == {"pending": None}


def test_pending_approval_undecided(monkeypatch, reset_service_singleton):
    record = _pending_record()
    monkeypatch.setattr(api_main.approvals, "peek", lambda: record)
    client = TestClient(build_app())
    response = client.get("/pending-approval")
    assert response.status_code == 200
    body = response.json()
    assert body["pending"] == {
        "id": "abc123",
        "tool_name": "obsidian_write",
        "description": "Write a note",
        "requested_at": "2026-09-03T10:00:00Z",
        "expires_at": "2026-09-03T10:30:00Z",
        "decision": None,
        "decided_by": None,
    }


def test_allow_success(monkeypatch, reset_service_singleton):
    def fake_decide(pending_id, decision, *, decided_by, **kwargs):
        fake_decide.calls.append((pending_id, decision, decided_by))
        return True

    fake_decide.calls = []
    monkeypatch.setattr(api_main.approvals, "decide", fake_decide)
    client = TestClient(build_app())
    response = client.post("/allow", json={"id": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert fake_decide.calls == [("abc123", "allow", "hud")]


def test_deny_success(monkeypatch, reset_service_singleton):
    def fake_decide(pending_id, decision, *, decided_by, **kwargs):
        fake_decide.calls.append((pending_id, decision, decided_by))
        return True

    fake_decide.calls = []
    monkeypatch.setattr(api_main.approvals, "decide", fake_decide)
    client = TestClient(build_app())
    response = client.post("/deny", json={"id": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert fake_decide.calls == [("abc123", "deny", "hud")]


@pytest.mark.parametrize("path", ["/allow", "/deny"])
def test_decision_rejected_returns_409(monkeypatch, reset_service_singleton, path):
    monkeypatch.setattr(api_main.approvals, "decide", lambda *a, **k: False)
    client = TestClient(build_app())
    response = client.post(path, json={"id": "abc123"})
    assert response.status_code == 409
    assert response.json() == {"error": "expired_or_mismatched"}


def test_allow_without_id_does_not_call_decide(
    monkeypatch, reset_service_singleton
):
    def explode(*a, **k):
        raise AssertionError("approvals.decide should not be called")

    monkeypatch.setattr(api_main.approvals, "decide", explode)
    client = TestClient(build_app())
    response = client.post("/allow", json={})
    assert response.status_code == 409
    assert response.json() == {"error": "expired_or_mismatched"}
    response = client.post("/allow", json={"id": ""})
    assert response.status_code == 409
    assert response.json() == {"error": "expired_or_mismatched"}


def test_command_success_and_speak_default(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            FakeService.constructed += 1

        async def aanswer(self, text, *, speak=True, **kwargs):
            FakeService.calls.append((text, speak))
            return "done"

        async def aclose(self):
            FakeService.aclose_calls += 1

    FakeService.constructed = 0
    FakeService.calls = []
    FakeService.aclose_calls = 0
    monkeypatch.setattr(api_main, "JarvisService", FakeService)

    client = TestClient(build_app())
    response = client.post("/command", json={"text": "hello"})
    assert response.status_code == 200
    assert response.json() == {"response": "done"}
    assert FakeService.calls == [("hello", True)]


def test_command_explicit_speak_false(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            pass

        async def aanswer(self, text, *, speak=True, **kwargs):
            FakeService.speak = speak
            return "ok"

        async def aclose(self):
            pass

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    client = TestClient(build_app())
    response = client.post("/command", json={"text": "hi", "speak": False})
    assert response.status_code == 200
    assert FakeService.speak is False


@pytest.mark.parametrize("invalid_speak", ["false", 0, 1, [], {}, None])
def test_command_rejects_non_boolean_speak(
    monkeypatch, reset_service_singleton, invalid_speak
):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            raise AssertionError("should not construct service for bad request")

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    response = TestClient(build_app()).post(
        "/command", json={"text": "hello", "speak": invalid_speak}
    )
    assert response.status_code == 422
    assert response.json() == {"error": "invalid request"}
    assert api_main._service is None


@pytest.mark.parametrize("invalid_text", [123, [], {}, None])
def test_command_rejects_non_string_text(
    monkeypatch, reset_service_singleton, invalid_text
):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            raise AssertionError("should not construct service for bad request")

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    response = TestClient(build_app()).post(
        "/command", json={"text": invalid_text}
    )
    assert response.status_code == 422
    if invalid_text is None:
        assert response.json() == {"error": "text is required"}
    else:
        assert response.json() == {"error": "invalid request"}
    assert api_main._service is None


def test_command_reuses_single_service_instance(
    monkeypatch, reset_service_singleton
):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    constructed = []

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            constructed.append(voice_model_path)

        async def aanswer(self, text, *, speak=True, **kwargs):
            return f"answered:{text}"

        async def aclose(self):
            pass

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    client = TestClient(build_app())
    assert client.post("/command", json={"text": "one"}).json() == {
        "response": "answered:one"
    }
    assert client.post("/command", json={"text": "two"}).json() == {
        "response": "answered:two"
    }
    assert len(constructed) == 1
    assert constructed == ["/tmp/voice.onnx"]


def test_command_missing_text(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            raise AssertionError("should not construct service for bad request")

        async def aanswer(self, text, *, speak=True):
            raise AssertionError("unreachable")

        async def aclose(self):
            pass

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    client = TestClient(build_app())
    for body in [{}, {"text": ""}]:
        response = client.post("/command", json=body)
        assert response.status_code == 422
        assert response.json() == {"error": "text is required"}
    assert api_main._service is None


def test_command_voice_model_not_configured(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", None, raising=True)
    constructed = []

    class FakeService:
        def __init__(self, voice_model_path):
            constructed.append(voice_model_path)

        async def aanswer(self, text, *, speak=True):
            raise AssertionError("unreachable")

        async def aclose(self):
            pass

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    client = TestClient(build_app())
    response = client.post("/command", json={"text": "hello"})
    assert response.status_code == 503
    assert response.json() == {"error": "voice_model_not_configured"}
    assert constructed == []
    assert api_main._service is None


def test_command_aanswer_raises_returns_502(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)

    class FakeService:
        async def aanswer(self, text, *, speak=True, **kwargs):
            raise RuntimeError("piper exploded")

        async def aclose(self):
            pass

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    client = TestClient(build_app())
    response = client.post("/command", json={"text": "hello"})
    assert response.status_code == 502
    assert response.json() == {"error": "command_failed"}


def test_command_constructor_uses_api_runtime_source_and_records_safe_error(
    monkeypatch, reset_service_singleton
):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    constructor_args = []
    recorded_errors = []

    class FakeService:
        def __init__(self, voice_model_path, runtime_source):
            constructor_args.append((voice_model_path, runtime_source))
            raise RuntimeError("sensitive constructor detail")

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    monkeypatch.setattr(
        api_main.runtime_status,
        "record_error",
        lambda source, code: recorded_errors.append((source, code)),
    )
    response = TestClient(build_app()).post("/command", json={"text": "hello"})
    assert response.status_code == 502
    assert response.json() == {"error": "command_failed"}
    assert constructor_args == [("/tmp/voice.onnx", "api")]
    assert recorded_errors == [("api", "service_unavailable")]


def test_concurrent_commands_serialize_service_turns(
    monkeypatch, reset_service_singleton
):
    async def run():
        monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        intervals = {}

        class FakeService:
            def __init__(self, voice_model_path, runtime_source="api"):
                pass

            async def aanswer(self, text, *, speak=True, **kwargs):
                loop = asyncio.get_running_loop()
                started = loop.time()
                intervals[text] = [started, None]
                if text == "one":
                    first_started.set()
                    await release_first.wait()
                intervals[text][1] = loop.time()
                return f"answered:{text}"

            async def aclose(self):
                pass

        monkeypatch.setattr(api_main, "JarvisService", FakeService)
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/command", json={"text": "one"}))
            await first_started.wait()
            second = asyncio.create_task(client.post("/command", json={"text": "two"}))
            await asyncio.sleep(0)
            assert set(intervals) == {"one"}
            release_first.set()
            first_response, second_response = await asyncio.gather(first, second)

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert first_response.json() == {"response": "answered:one"}
        assert second_response.json() == {"response": "answered:two"}
        assert intervals["two"][0] >= intervals["one"][1]

    asyncio.run(run())


def test_shutdown_waits_for_active_command_turn(
    monkeypatch, reset_service_singleton
):
    async def run():
        monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
        command_started = asyncio.Event()
        release_command = asyncio.Event()
        aclose_called = asyncio.Event()

        class FakeService:
            def __init__(self, voice_model_path, runtime_source="api"):
                pass

            async def aanswer(self, text, *, speak=True, **kwargs):
                command_started.set()
                await release_command.wait()
                return "finished"

            async def aclose(self):
                aclose_called.set()

        monkeypatch.setattr(api_main, "JarvisService", FakeService)
        lifecycle = api_main._lifespan(build_app())
        await lifecycle.__aenter__()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = asyncio.create_task(client.post("/command", json={"text": "hello"}))
            await command_started.wait()
            shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
            await asyncio.sleep(0)
            assert not aclose_called.is_set()
            release_command.set()
            assert (await request).json() == {"response": "finished"}
            await shutdown

        assert aclose_called.is_set()

    asyncio.run(run())


def test_client_shutdown_closes_service(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    aclose_calls = []

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            pass

        async def aanswer(self, text, *, speak=True, **kwargs):
            return "ok"

        async def aclose(self):
            aclose_calls.append(True)

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    with TestClient(build_app()) as client:
        assert client.post("/command", json={"text": "hello"}).status_code == 200
    assert aclose_calls == [True]


def test_client_shutdown_without_service_does_not_call_aclose(
    monkeypatch, reset_service_singleton
):
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    aclose_calls = []

    class FakeService:
        def __init__(self, voice_model_path, runtime_source="api"):
            pass

        async def aclose(self):
            aclose_calls.append(True)

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    with TestClient(build_app()) as client:
        assert client.get("/vitals").status_code == 200
    assert aclose_calls == []


# --------------------------------------------------------------------------- #
# POST /voice-turn (Task 7)
# --------------------------------------------------------------------------- #

import numpy as np  # noqa: E402

from jarvis.orchestrator.service import NoSpeechDetectedError  # noqa: E402

TURN_ID = "123e4567-e89b-42d3-a456-426614174000"


@pytest.fixture
def browser_mode(monkeypatch, reset_service_singleton):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "browser", raising=True)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    monkeypatch.setattr(config, "HUD_ORIGIN", None, raising=True)
    return monkeypatch


def _install_fake_service(monkeypatch, *, arun_audio=None):
    calls = []

    class FakeService:
        constructed = 0

        def __init__(self, voice_model_path, runtime_source="api"):
            FakeService.constructed += 1

        async def arun_audio(self, audio):
            calls.append(audio)
            if arun_audio is not None:
                return await arun_audio(audio)
            return "hello there", "general kenobi"

        async def aanswer(self, text, *, speak=True, **kwargs):
            return "typed"

        async def aclose(self):
            pass

    FakeService.calls = calls
    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    return FakeService


def _fake_decode(monkeypatch, result=None, raises=None):
    seen = []

    def decode(data, content_type):
        seen.append((data, content_type, threading.get_ident()))
        if raises is not None:
            raise raises
        return result if result is not None else np.zeros(16, dtype=np.float32)

    monkeypatch.setattr(api_main.voice_media, "decode_media", decode)
    return seen


def _asgi_post(path_query, headers, chunks):
    """Drive the app directly so Content-Length can be omitted or dishonest."""
    query = ""
    path = path_query
    if "?" in path_query:
        path, query = path_query.split("?", 1)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    sent = {}
    body = bytearray()

    async def receive():
        if pending:
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {
                k.decode().lower(): v.decode() for k, v in message["headers"]
            }
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(build_app()(scope, receive, send))
    import json as _json

    return sent["status"], sent["headers"], _json.loads(bytes(body))


def test_voice_turn_happy_path(browser_mode):
    seen = _fake_decode(browser_mode)
    fake = _install_fake_service(browser_mode)
    client = TestClient(build_app())
    response = client.post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"audio-bytes",
        headers={"Content-Type": "audio/webm;codecs=opus"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "turn_id": TURN_ID,
        "transcript": "hello there",
        "response": "general kenobi",
    }
    assert response.headers["cache-control"] == "no-store"
    assert seen[0][:2] == (b"audio-bytes", "audio/webm;codecs=opus")
    assert len(fake.calls) == 1
    assert api_main._active_voice_turn is None
    assert not api_main._command_turn_lock.locked()


def test_voice_turn_decode_runs_off_event_loop_thread(browser_mode):
    seen = _fake_decode(browser_mode)
    _install_fake_service(browser_mode)
    main_thread = threading.get_ident()
    client = TestClient(build_app())
    client.post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/ogg"},
    )
    assert seen and seen[0][2] != main_thread


def test_voice_turn_non_browser_mode_unavailable(monkeypatch, reset_service_singleton):
    for mode in ("terminal", "off"):
        monkeypatch.setattr(config, "VOICE_INPUT_MODE", mode, raising=True)
        monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
        response = TestClient(build_app()).post(
            f"/voice-turn?turn_id={TURN_ID}",
            content=b"x",
            headers={"Content-Type": "audio/webm"},
        )
        assert response.status_code == 503
        assert response.json() == {"error": "browser_voice_unavailable"}


def test_voice_turn_missing_model(browser_mode):
    browser_mode.setattr(config, "VOICE_MODEL_PATH", None, raising=True)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 503
    assert response.json() == {"error": "voice_model_not_configured"}


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?turn_id=",
        "?turn_id=not-a-uuid",
        "?turn_id=123E4567-E89B-42D3-A456-426614174000",
        "?turn_id=123e4567e89b42d3a456426614174000",
        "?turn_id=%7B123e4567-e89b-42d3-a456-426614174000%7D",
        "?turn_id=123e4567-e89b-42d3-a456-426614174000%20",
        "?turn_id=123e4567-e89b-42d3-a456-42661417400%C3%A9",
        "?turn_id=" + "a" * 5000,
    ],
)
def test_voice_turn_invalid_turn_id(browser_mode, query):
    seen = _fake_decode(browser_mode)
    fake = _install_fake_service(browser_mode)
    response = TestClient(build_app()).post(
        "/voice-turn" + query,
        content=b"x",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_turn_id"}
    assert seen == [] and fake.calls == [] and fake.constructed == 0
    assert api_main._active_voice_turn is None


def test_voice_turn_declared_length_over_limit_rejected_before_iteration(browser_mode):
    seen = _fake_decode(browser_mode)
    _install_fake_service(browser_mode)
    status, _, body = _asgi_post(
        f"/voice-turn?turn_id={TURN_ID}",
        {
            "content-type": "audio/webm",
            "content-length": str(voice_media.MAX_UPLOAD_BYTES + 1),
        },
        [b"tiny"],
    )
    assert status == 413
    assert body == {"error": "audio_too_large"}
    assert seen == []


@pytest.mark.parametrize("declared", [None, "abc", "-5", "3"])
def test_voice_turn_actual_bytes_bound_even_with_bad_length(browser_mode, declared):
    browser_mode.setattr(api_main.voice_media, "MAX_UPLOAD_BYTES", 10, raising=True)
    seen = _fake_decode(browser_mode)
    _install_fake_service(browser_mode)
    headers = {"content-type": "audio/webm"}
    if declared is not None:
        headers["content-length"] = declared
    status, _, body = _asgi_post(
        f"/voice-turn?turn_id={TURN_ID}", headers, [b"123456", b"789012"]
    )
    assert status == 413
    assert body == {"error": "audio_too_large"}
    assert seen == []
    assert api_main._active_voice_turn is None
    assert not api_main._command_turn_lock.locked()


def test_voice_turn_exact_limit_accepted(browser_mode):
    browser_mode.setattr(api_main.voice_media, "MAX_UPLOAD_BYTES", 10, raising=True)
    seen = _fake_decode(browser_mode)
    _install_fake_service(browser_mode)
    status, _, body = _asgi_post(
        f"/voice-turn?turn_id={TURN_ID}",
        {"content-type": "audio/webm"},
        [b"12345", b"67890"],
    )
    assert status == 200
    assert seen[0][0] == b"1234567890"


@pytest.mark.parametrize(
    "error, status, code",
    [
        (voice_media.UnsupportedMediaTypeError("secret"), 415, "unsupported_audio_type"),
        (voice_media.EmptyAudioError("secret"), 422, "empty_audio"),
        (voice_media.MediaTooLargeError("secret"), 413, "audio_too_large"),
        (voice_media.NoDecoderError("secret"), 503, "decoder_unavailable"),
        (voice_media.DecodeTimeoutError("secret"), 504, "audio_decode_timeout"),
        (voice_media.DecodeError("secret"), 422, "audio_decode_failed"),
        (voice_media.AudioDurationOverflowError("secret"), 413, "audio_too_long"),
    ],
)
def test_voice_turn_decode_error_mappings(browser_mode, error, status, code):
    _fake_decode(browser_mode, raises=error)
    fake = _install_fake_service(browser_mode)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == status
    assert response.json() == {"error": code}
    assert "secret" not in response.text
    assert fake.calls == []
    assert api_main._active_voice_turn is None
    assert not api_main._command_turn_lock.locked()


def test_voice_turn_real_decoder_empty_body_is_422(browser_mode):
    _install_fake_service(browser_mode)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "empty_audio"}


def test_voice_turn_real_decoder_unsupported_type_is_415(browser_mode):
    _install_fake_service(browser_mode)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/wav"},
    )
    assert response.status_code == 415
    assert response.json() == {"error": "unsupported_audio_type"}


def test_voice_turn_no_speech(browser_mode):
    _fake_decode(browser_mode)

    async def no_speech(audio):
        raise NoSpeechDetectedError("secret transcript")

    _install_fake_service(browser_mode, arun_audio=no_speech)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "no_speech_detected"}
    assert api_main._active_voice_turn is None


def test_voice_turn_service_failure_is_content_free(browser_mode):
    _fake_decode(browser_mode)

    async def boom(audio):
        raise RuntimeError("secret sdk detail")

    _install_fake_service(browser_mode, arun_audio=boom)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 502
    assert response.json() == {"error": "voice_turn_failed"}
    assert "secret" not in response.text
    assert not api_main._command_turn_lock.locked()


def test_voice_turn_service_construction_failure(browser_mode):
    _fake_decode(browser_mode)
    recorded = []
    browser_mode.setattr(
        api_main.runtime_status, "record_error", lambda *a: recorded.append(a)
    )

    class BadService:
        def __init__(self, *a, **k):
            raise RuntimeError("secret")

    browser_mode.setattr(api_main, "JarvisService", BadService)
    response = TestClient(build_app()).post(
        f"/voice-turn?turn_id={TURN_ID}",
        content=b"x",
        headers={"Content-Type": "audio/webm"},
    )
    assert response.status_code == 502
    assert response.json() == {"error": "voice_turn_failed"}
    assert recorded == [("api", "service_unavailable")]


def test_voice_turn_fails_fast_when_lock_held_and_second_turn_rejected(browser_mode):
    async def run():
        _fake_decode(browser_mode)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(audio):
            started.set()
            await release.wait()
            return "t", "r"

        fake = _install_fake_service(browser_mode, arun_audio=slow)
        transport = httpx.ASGITransport(app=build_app())
        other = str(uuid.uuid4())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(
                client.post(
                    f"/voice-turn?turn_id={TURN_ID}",
                    content=b"x",
                    headers={"Content-Type": "audio/webm"},
                )
            )
            await started.wait()
            assert api_main._active_voice_turn.turn_id == TURN_ID
            assert isinstance(api_main._active_voice_turn.task, asyncio.Task)
            second = await client.post(
                f"/voice-turn?turn_id={other}",
                content=b"x",
                headers={"Content-Type": "audio/webm"},
            )
            assert second.status_code == 409
            assert second.json() == {"error": "turn_in_progress"}
            assert len(fake.calls) == 1
            assert api_main._active_voice_turn.turn_id == TURN_ID
            release.set()
            done = await first
        assert done.status_code == 200
        assert api_main._active_voice_turn is None
        assert not api_main._command_turn_lock.locked()

    asyncio.run(run())


def test_voice_turn_fails_fast_when_command_holds_lock(browser_mode):
    async def run():
        _fake_decode(browser_mode)
        _install_fake_service(browser_mode)
        await api_main._command_turn_lock.acquire()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.post(
                    f"/voice-turn?turn_id={TURN_ID}",
                    content=b"x",
                    headers={"Content-Type": "audio/webm"},
                ),
                timeout=2,
            )
        assert response.status_code == 409
        assert response.json() == {"error": "turn_in_progress"}
        assert api_main._command_turn_lock.locked()
        api_main._command_turn_lock.release()

    asyncio.run(run())


def test_voice_turn_event_loop_stays_responsive_during_stt(browser_mode):
    async def run():
        _fake_decode(browser_mode)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(audio):
            started.set()
            await release.wait()
            return "t", "r"

        _install_fake_service(browser_mode, arun_audio=slow)
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(
                client.post(
                    f"/voice-turn?turn_id={TURN_ID}",
                    content=b"x",
                    headers={"Content-Type": "audio/webm"},
                )
            )
            await started.wait()
            capabilities = await asyncio.wait_for(client.get("/vitals"), timeout=2)
            assert capabilities.status_code == 200
            release.set()
            await first

    asyncio.run(run())


def test_command_unchanged_after_voice_turn(browser_mode):
    _fake_decode(browser_mode)
    _install_fake_service(browser_mode)
    client = TestClient(build_app())
    assert (
        client.post(
            f"/voice-turn?turn_id={TURN_ID}",
            content=b"x",
            headers={"Content-Type": "audio/webm"},
        ).status_code
        == 200
    )
    response = client.post("/command", json={"text": "hi"})
    assert response.status_code == 200
    assert response.json() == {"response": "typed"}


# --------------------------------------------------------------------------- #
# Exact-turn cancellation, deadline, shutdown ordering (Task 8)
# --------------------------------------------------------------------------- #

import functools  # noqa: E402

from jarvis.approvals import store as approvals_store  # noqa: E402
from jarvis.orchestrator import permissions  # noqa: E402

OTHER_ID = "223e4567-e89b-42d3-a456-426614174999"
AUDIO_HEADERS = {"Content-Type": "audio/webm"}


class _Harness:
    def __init__(self, monkeypatch, behavior):
        self.events = []
        self.cancel_count = 0
        self.started = asyncio.Event()
        self.constructed = 0
        self.aclose_calls = 0
        harness = self

        class FakeService:
            def __init__(self, voice_model_path, runtime_source="api"):
                harness.constructed += 1

            async def arun_audio(self, audio):
                harness.started.set()
                try:
                    return await behavior(harness)
                except asyncio.CancelledError:
                    harness.cancel_count += 1
                    harness.events.append("worker_cancelled")
                    await asyncio.sleep(0.02)
                    harness.events.append("worker_cleaned")
                    raise

            async def aanswer(self, text, *, speak=True, **kwargs):
                return "typed"

            async def aclose(self):
                harness.aclose_calls += 1
                harness.events.append("aclose")

        monkeypatch.setattr(api_main, "JarvisService", FakeService)
        _fake_decode(monkeypatch)


async def _wait_forever(harness):
    await asyncio.Event().wait()


async def _stt_thread_wait(harness):
    release = threading.Event()
    harness.release_thread = release
    try:
        await asyncio.to_thread(release.wait, 5)
    finally:
        release.set()
    return "t", "r"


def _post_turn(client, turn_id=TURN_ID):
    return asyncio.create_task(
        client.post(f"/voice-turn?turn_id={turn_id}", content=b"x", headers=AUDIO_HEADERS)
    )


def _run_with_client(coro_fn):
    async def run():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda l, ctx: errors.append(ctx))
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await coro_fn(client)
        await asyncio.sleep(0)
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftovers == []
        assert errors == []
        assert api_main._active_voice_turn is None
        assert not api_main._command_turn_lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("behavior", [_wait_forever, _stt_thread_wait])
def test_voice_cancel_during_stt_or_sdk_wait(browser_mode, behavior):
    harness = _Harness(browser_mode, behavior)

    async def scenario(client):
        turn = _post_turn(client)
        await harness.started.wait()
        cancel = await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        assert cancel.status_code == 200
        assert cancel.json() == {"cancelled": True}
        # cancel returns only after cleanup completed
        assert api_main._active_voice_turn is None
        assert not api_main._command_turn_lock.locked()
        assert harness.events == ["worker_cancelled", "worker_cleaned", "aclose"]
        assert api_main._service is None
        response = await turn
        assert response.status_code == 409
        assert response.json() == {"error": "voice_turn_cancelled"}

    _run_with_client(scenario)
    assert harness.cancel_count == 1


def test_voice_cancel_isolated_to_exact_id_and_not_typed_commands(browser_mode):
    harness = _Harness(browser_mode, _wait_forever)

    async def scenario(client):
        turn = _post_turn(client)
        await harness.started.wait()
        for bad in (
            {"turn_id": OTHER_ID},
            {"turn_id": TURN_ID.upper()},
            {"turn_id": 5},
            {"turn_id": TURN_ID, "extra": 1},
            {},
            [TURN_ID],
        ):
            resp = await client.post("/voice-cancel", json=bad)
            assert resp.status_code == 409
            assert resp.json() == {"error": "no_matching_turn"}
        resp = await client.post(
            "/voice-cancel", content=b"not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 409
        assert harness.cancel_count == 0
        assert not turn.done()
        assert api_main._active_voice_turn.turn_id == TURN_ID
        await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        await turn

    _run_with_client(scenario)


def test_voice_cancel_does_not_cancel_typed_command(browser_mode):
    async def scenario(client):
        started = asyncio.Event()
        release = asyncio.Event()

        class FakeService:
            def __init__(self, *a, **k):
                pass

            async def aanswer(self, text, *, speak=True, **kwargs):
                started.set()
                await release.wait()
                return "typed-done"

            async def aclose(self):
                pass

        browser_mode.setattr(api_main, "JarvisService", FakeService)
        command = asyncio.create_task(client.post("/command", json={"text": "hi"}))
        await started.wait()
        resp = await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        assert resp.status_code == 409
        assert resp.json() == {"error": "no_matching_turn"}
        assert not command.done()
        release.set()
        assert (await command).json() == {"response": "typed-done"}

    _run_with_client(scenario)


def test_voice_cancel_stale_and_double_cancel(browser_mode):
    harness = _Harness(browser_mode, _wait_forever)

    async def scenario(client):
        turn = _post_turn(client)
        await harness.started.wait()
        first, second = await asyncio.gather(
            client.post("/voice-cancel", json={"turn_id": TURN_ID}),
            client.post("/voice-cancel", json={"turn_id": TURN_ID}),
        )
        assert first.json() == {"cancelled": True}
        assert second.json() == {"cancelled": True}
        assert harness.cancel_count == 1
        assert harness.aclose_calls == 1
        await turn
        stale = await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        assert stale.status_code == 409
        assert stale.json() == {"error": "no_matching_turn"}

    _run_with_client(scenario)


def test_voice_cancel_after_completion_is_no_match(browser_mode):
    async def quick(harness):
        return "t", "r"

    _Harness(browser_mode, quick)

    async def scenario(client):
        resp = await _post_turn(client)
        assert resp.status_code == 200
        stale = await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        assert stale.status_code == 409

    _run_with_client(scenario)


def test_voice_deadline_cancels_once_and_cleans_up(browser_mode):
    browser_mode.setattr(api_main, "_VOICE_TURN_TIMEOUT_SECONDS", 0.05)
    harness = _Harness(browser_mode, _wait_forever)

    async def scenario(client):
        resp = await _post_turn(client)
        assert resp.status_code == 504
        assert resp.json() == {"error": "voice_turn_timeout"}
        assert harness.events == ["worker_cancelled", "worker_cleaned", "aclose"]
        assert api_main._service is None

    _run_with_client(scenario)
    assert harness.cancel_count == 1


def test_voice_cancel_and_deadline_race_single_cancellation(browser_mode):
    browser_mode.setattr(api_main, "_VOICE_TURN_TIMEOUT_SECONDS", 0.01)
    harness = _Harness(browser_mode, _wait_forever)

    async def scenario(client):
        turn = _post_turn(client)
        await harness.started.wait()
        cancel = asyncio.create_task(client.post("/voice-cancel", json={"turn_id": TURN_ID}))
        cancel_resp = await cancel
        resp = await turn
        assert cancel_resp.status_code == 200
        assert resp.status_code in (409, 504)

    _run_with_client(scenario)
    assert harness.cancel_count == 1
    assert harness.aclose_calls == 1


def test_voice_cancel_and_shutdown_race(browser_mode):
    harness = _Harness(browser_mode, _wait_forever)

    async def run():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda l, ctx: errors.append(ctx))
        lifecycle = api_main._lifespan(build_app())
        await lifecycle.__aenter__()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            turn = _post_turn(client)
            await harness.started.wait()
            cancel = asyncio.create_task(
                client.post("/voice-cancel", json={"turn_id": TURN_ID})
            )
            shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
            await asyncio.wait_for(asyncio.gather(cancel, shutdown, turn), timeout=5)
        await asyncio.sleep(0)
        assert harness.cancel_count == 1
        assert harness.aclose_calls == 1
        assert api_main._active_voice_turn is None
        assert not api_main._command_turn_lock.locked()
        assert errors == []

    asyncio.run(run())


def test_voice_shutdown_cancels_active_turn_before_closing_service(browser_mode):
    harness = _Harness(browser_mode, _wait_forever)

    async def run():
        lifecycle = api_main._lifespan(build_app())
        await lifecycle.__aenter__()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            turn = _post_turn(client)
            await harness.started.wait()
            await asyncio.wait_for(lifecycle.__aexit__(None, None, None), timeout=5)
            resp = await turn
        assert resp.status_code == 409
        assert harness.events == ["worker_cancelled", "worker_cleaned", "aclose"]
        assert harness.aclose_calls == 1

    asyncio.run(run())


def test_voice_shutdown_without_turn_or_service(browser_mode):
    async def run():
        lifecycle = api_main._lifespan(build_app())
        await lifecycle.__aenter__()
        await lifecycle.__aexit__(None, None, None)

    asyncio.run(run())


def test_voice_handler_cancellation_finishes_cleanup_first(browser_mode):
    harness = _Harness(browser_mode, _wait_forever)

    async def scenario(client):
        turn = _post_turn(client)
        await harness.started.wait()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        # cleanup had fully completed before the cancellation propagated
        assert harness.events == ["worker_cancelled", "worker_cleaned", "aclose"]
        assert api_main._active_voice_turn is None
        assert not api_main._command_turn_lock.locked()
        assert api_main._service is None

    _run_with_client(scenario)
    assert harness.cancel_count == 1


def test_voice_new_turn_after_cancel_uses_fresh_service(browser_mode):
    calls = {"n": 0}

    async def behavior(harness):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.Event().wait()
        return "t", "r"

    harness = _Harness(browser_mode, behavior)

    async def scenario(client):
        first = _post_turn(client)
        await harness.started.wait()
        await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        await first
        second = await _post_turn(client, OTHER_ID)
        assert second.status_code == 200

    _run_with_client(scenario)
    assert harness.constructed == 2


def test_voice_cancel_during_approval_wait_clears_record_before_discard(
    browser_mode, tmp_path
):
    data_path = tmp_path / "pending_approval.json"
    lock_path = tmp_path / "pending_approval.lock"
    for name in ("claim", "peek", "clear", "decide"):
        original = getattr(approvals_store, name)
        browser_mode.setattr(
            approvals_store,
            name,
            functools.partial(original, data_path=data_path, lock_path=lock_path),
        )
    browser_mode.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)

    async def no_terminal(description):
        await asyncio.Event().wait()

    browser_mode.setattr(permissions, "_wait_terminal", no_terminal)
    observed = {}

    async def behavior(harness):
        async def watch():
            while approvals_store.peek() is None:
                await asyncio.sleep(0.005)
            observed["claimed"] = approvals_store.peek()
            harness.events.append("approval_claimed")

        watcher = asyncio.create_task(watch())
        original_clear = approvals_store.clear

        def recording_clear(pending_id, **kwargs):
            harness.events.append("approval_cleared")
            return original_clear(pending_id, **kwargs)

        browser_mode.setattr(approvals_store, "clear", recording_clear)
        try:
            await permissions.pre_tool_use_hook(
                {
                    "tool_name": "mcp__obsidian__vault_write",
                    "tool_input": {"path": "a.md", "content": "x"},
                },
                None,
                None,
            )
        finally:
            await watcher
        return "t", "r"

    harness = _Harness(browser_mode, behavior)

    async def scenario(client):
        turn = _post_turn(client)
        await harness.started.wait()
        for _ in range(200):
            if "approval_claimed" in harness.events:
                break
            await asyncio.sleep(0.005)
        assert approvals_store.peek() is not None
        cancel = await client.post("/voice-cancel", json={"turn_id": TURN_ID})
        assert cancel.json() == {"cancelled": True}
        assert approvals_store.peek() is None
        events = harness.events
        assert events.index("approval_cleared") < events.index("aclose")
        assert events.index("worker_cleaned") < events.index("aclose")
        await turn

    _run_with_client(scenario)
    assert not data_path.exists() or approvals_store.peek(
        data_path=data_path, lock_path=lock_path
    ) is None


# --------------------------------------------------------------------------- #
# POST /voice-speech (Task 9)
# --------------------------------------------------------------------------- #


class _FakeSpeaker:
    instances = []

    def __init__(self, model_path):
        self.model_path = model_path
        self.calls = []
        self.threads = []
        self.say_calls = 0
        self.wav = b"RIFF-fake-wav"
        self.error = None
        self.gate = None
        _FakeSpeaker.instances.append(self)

    def synthesize_wav(self, text):
        self.calls.append(text)
        self.threads.append(threading.get_ident())
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.wav

    def say(self, text):
        self.say_calls += 1
        raise AssertionError("say must never be called")


@pytest.fixture
def speech_mode(browser_mode):
    _FakeSpeaker.instances = []
    browser_mode.setattr(api_main, "Speaker", _FakeSpeaker)
    return browser_mode


def _speak(client, payload):
    return client.post("/voice-speech", json=payload)


def test_voice_speech_happy_path_exact_bytes_and_headers(speech_mode):
    client = TestClient(build_app())
    main_thread = threading.get_ident()
    response = _speak(client, {"text": "hello world"})
    assert response.status_code == 200
    assert response.content == b"RIFF-fake-wav"
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    speaker = _FakeSpeaker.instances[0]
    assert speaker.calls == ["hello world"]
    assert speaker.threads[0] != main_thread
    assert speaker.model_path == "/tmp/voice.onnx"
    assert speaker.say_calls == 0
    assert not api_main._speech_lock.locked()


def test_voice_speech_reuses_one_speaker(speech_mode):
    client = TestClient(build_app())
    assert _speak(client, {"text": "one"}).status_code == 200
    assert _speak(client, {"text": "two"}).status_code == 200
    assert len(_FakeSpeaker.instances) == 1
    assert _FakeSpeaker.instances[0].calls == ["one", "two"]


def test_voice_speech_accepts_limit_and_rejects_over(speech_mode):
    client = TestClient(build_app())
    assert _speak(client, {"text": "a" * 10000}).status_code == 200
    # code points, not bytes: multi-byte characters count once
    assert _speak(client, {"text": "\u00e9" * 10000}).status_code == 200
    response = _speak(client, {"text": "a" * 10001})
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_speech_text"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": ""},
        {"text": None},
        {"text": 5},
        {"text": ["x"]},
        {"text": "x", "speak": True},
        {"txt": "x"},
        ["x"],
        "x",
        5,
        None,
    ],
)
def test_voice_speech_rejects_invalid_shapes(speech_mode, payload):
    response = _speak(TestClient(build_app()), payload)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_speech_text"}
    assert _FakeSpeaker.instances == []


def test_voice_speech_rejects_malformed_json(speech_mode):
    response = TestClient(build_app()).post(
        "/voice-speech", content=b"{nope", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_speech_text"}


def test_voice_speech_mode_gate(monkeypatch, reset_service_singleton):
    _FakeSpeaker.instances = []
    monkeypatch.setattr(api_main, "Speaker", _FakeSpeaker)
    monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
    for mode in ("terminal", "off"):
        monkeypatch.setattr(config, "VOICE_INPUT_MODE", mode, raising=True)
        response = _speak(TestClient(build_app()), {"text": "hi"})
        assert response.status_code == 503
        assert response.json() == {"error": "browser_voice_unavailable"}
    assert _FakeSpeaker.instances == []


def test_voice_speech_model_gate(speech_mode):
    speech_mode.setattr(config, "VOICE_MODEL_PATH", None, raising=True)
    response = _speak(TestClient(build_app()), {"text": "hi"})
    assert response.status_code == 503
    assert response.json() == {"error": "voice_model_not_configured"}
    assert _FakeSpeaker.instances == []


def test_voice_speech_failure_is_content_free(speech_mode):
    client = TestClient(build_app())
    assert _speak(client, {"text": "warm"}).status_code == 200
    _FakeSpeaker.instances[0].error = RuntimeError("secret piper detail")
    response = _speak(client, {"text": "hi"})
    assert response.status_code == 502
    assert response.json() == {"error": "speech_failed"}
    assert "secret" not in response.text
    assert not api_main._speech_lock.locked()


def test_voice_speech_speaker_construction_failure(speech_mode):
    class Bad:
        def __init__(self, *a):
            raise RuntimeError("secret")

    speech_mode.setattr(api_main, "Speaker", Bad)
    response = _speak(TestClient(build_app()), {"text": "hi"})
    assert response.status_code == 502
    assert response.json() == {"error": "speech_failed"}
    assert not api_main._speech_lock.locked()


def test_voice_speech_fail_fast_busy_and_event_loop_free(speech_mode):
    async def run():
        gate = threading.Event()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            warm = await client.post("/voice-speech", json={"text": "warm"})
            assert warm.status_code == 200
            speaker = _FakeSpeaker.instances[0]
            speaker.gate = gate
            first = asyncio.create_task(client.post("/voice-speech", json={"text": "a"}))
            for _ in range(200):
                if len(speaker.calls) == 2:
                    break
                await asyncio.sleep(0.005)
            assert len(speaker.calls) == 2
            busy = await asyncio.wait_for(
                client.post("/voice-speech", json={"text": "b"}), timeout=2
            )
            assert busy.status_code == 409
            assert busy.json() == {"error": "speech_busy"}
            vitals_resp = await asyncio.wait_for(client.get("/vitals"), timeout=2)
            assert vitals_resp.status_code == 200
            gate.set()
            assert (await first).status_code == 200
        assert len(speaker.calls) == 2
        assert not api_main._speech_lock.locked()

    asyncio.run(run())


def test_voice_speech_independent_of_command_and_turn_locks(speech_mode):
    async def run():
        await api_main._command_turn_lock.acquire()
        api_main._active_voice_turn = api_main._VoiceTurn(TURN_ID)
        fake_service_constructed = []
        speech_mode.setattr(
            api_main,
            "JarvisService",
            lambda *a, **k: fake_service_constructed.append(1),
        )
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.post("/voice-speech", json={"text": "hi"}), timeout=2
            )
        assert response.status_code == 200
        assert fake_service_constructed == []
        assert api_main._service is None
        assert api_main._command_turn_lock.locked()
        api_main._command_turn_lock.release()
        api_main._active_voice_turn = None

    asyncio.run(run())


def test_voice_speech_holds_lock_until_worker_finishes_on_disconnect(speech_mode):
    async def run():
        gate = threading.Event()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/voice-speech", json={"text": "warm"})
            speaker = _FakeSpeaker.instances[0]
            speaker.gate = gate
            first = asyncio.create_task(client.post("/voice-speech", json={"text": "a"}))
            for _ in range(200):
                if len(speaker.calls) == 2:
                    break
                await asyncio.sleep(0.005)
            first.cancel()
            await asyncio.sleep(0.02)
            assert not first.done()  # still waiting for native worker
            assert api_main._speech_lock.locked()
            busy = await client.post("/voice-speech", json={"text": "b"})
            assert busy.status_code == 409
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await first
        assert not api_main._speech_lock.locked()

    asyncio.run(run())


def test_voice_speech_does_not_touch_storage_or_runtime(speech_mode):
    touched = []
    speech_mode.setattr(
        api_main.runtime_status, "record_error", lambda *a: touched.append(a)
    )
    speech_mode.setattr(api_main.store, "read_recent", lambda n: touched.append(n))
    assert _speak(TestClient(build_app()), {"text": "hi"}).status_code == 200
    assert touched == []


def test_shutdown_waits_for_speech_lock_then_drops_speaker(speech_mode):
    async def run():
        gate = threading.Event()
        lifecycle = api_main._lifespan(build_app())
        await lifecycle.__aenter__()
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/voice-speech", json={"text": "warm"})
            speaker = _FakeSpeaker.instances[0]
            assert api_main._speaker is speaker
            speaker.gate = gate
            request = asyncio.create_task(client.post("/voice-speech", json={"text": "a"}))
            for _ in range(200):
                if len(speaker.calls) == 2:
                    break
                await asyncio.sleep(0.005)
            shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
            await asyncio.sleep(0.05)
            assert not shutdown.done()
            assert api_main._speaker is speaker
            gate.set()
            assert (await request).status_code == 200
            await asyncio.wait_for(shutdown, timeout=2)
        assert api_main._speaker is None

    asyncio.run(run())


def test_shutdown_without_speaker_is_fine(speech_mode):
    async def run():
        lifecycle = api_main._lifespan(build_app())
        await lifecycle.__aenter__()
        await lifecycle.__aexit__(None, None, None)
        assert api_main._speaker is None

    asyncio.run(run())
