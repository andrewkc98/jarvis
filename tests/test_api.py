import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import config, telemetry
from jarvis.api import launcher, main as api_main
from jarvis.api.main import app as the_app, build_app
from jarvis.approvals.store import PendingApproval
from jarvis.providers import schedule_provider, vault_provider
from jarvis.telemetry import store
from jarvis.tools import vitals


@pytest.fixture
def monkeypatched_client(monkeypatch):
    monkeypatch.setattr(config, "HUD_ORIGIN", None, raising=True)
    monkeypatch.setattr(
        schedule_provider,
        "get_upcoming_events",
        lambda: [
            {"summary": "Standup", "start": "2026-09-03T09:00:00Z", "end": "2026-09-03T09:15:00Z"}
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
            }
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
        def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
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


def test_concurrent_commands_serialize_service_turns(
    monkeypatch, reset_service_singleton
):
    async def run():
        monkeypatch.setattr(config, "VOICE_MODEL_PATH", "/tmp/voice.onnx", raising=True)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        intervals = {}

        class FakeService:
            def __init__(self, voice_model_path):
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
            def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
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
        def __init__(self, voice_model_path):
            pass

        async def aclose(self):
            aclose_calls.append(True)

    monkeypatch.setattr(api_main, "JarvisService", FakeService)
    with TestClient(build_app()) as client:
        assert client.get("/vitals").status_code == 200
    assert aclose_calls == []
