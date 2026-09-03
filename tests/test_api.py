import pytest
from fastapi.testclient import TestClient

from jarvis import config, telemetry
from jarvis.api import launcher
from jarvis.api.main import app as the_app, build_app
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
