import pytest

from jarvis.approvals import store as approvals_store
from jarvis.runtime import status as runtime_status
from jarvis.telemetry import store as telemetry_store


def _redirect_kwdefaults(monkeypatch, module, data_path, lock_path):
    """Repoint ``data_path``/``lock_path`` keyword defaults bound at import time."""
    for name, value in vars(module).items():
        defaults = getattr(value, "__kwdefaults__", None)
        if not callable(value) or not defaults:
            continue
        if getattr(value, "__module__", None) != module.__name__:
            continue
        if "data_path" in defaults:
            monkeypatch.setitem(defaults, "data_path", data_path)
        if "lock_path" in defaults:
            monkeypatch.setitem(defaults, "lock_path", lock_path)


@pytest.fixture(autouse=True)
def _isolate_runtime_stores(monkeypatch, tmp_path_factory):
    """Keep every test away from the repo-root telemetry/status/approval files."""
    root = tmp_path_factory.mktemp("jarvis_stores")

    _redirect_kwdefaults(
        monkeypatch,
        telemetry_store,
        root / "telemetry.jsonl",
        root / "telemetry.jsonl.lock",
    )
    _redirect_kwdefaults(
        monkeypatch,
        approvals_store,
        root / "pending_approval.json",
        root / "pending_approval.json.lock",
    )
    monkeypatch.setattr(
        runtime_status, "RUNTIME_STATUS_PATH", root / "runtime_status.json"
    )
    monkeypatch.setattr(
        runtime_status, "RUNTIME_STATUS_LOCK_PATH", root / "runtime_status.json.lock"
    )
    yield
