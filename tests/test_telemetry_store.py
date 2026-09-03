"""Tests for ``jarvis.telemetry.store``.

Concurrency tests spawn real OS-level child processes (``multiprocessing.Process``)
so they exercise ``fcntl.flock`` semantics across processes, not just
intra-process threads or ``asyncio`` tasks.
"""

from __future__ import annotations

import json
import multiprocessing
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.telemetry import store
from jarvis.telemetry.store import (
    ROTATION_LINE_THRESHOLD,
    SCHEMA_VERSION,
    TelemetryEntry,
    append_entry,
    parse_line,
    read_recent,
)


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _entry(**kwargs) -> TelemetryEntry:
    defaults = dict(
        timestamp=_iso(0.0),
        path="sdk",
        duration_ms=12.0,
        stt_ms=None,
        dispatch_ms=7.0,
        tools_fired=[],
        error=None,
    )
    defaults.update(kwargs)
    return TelemetryEntry(**defaults)


# ---------------------------------------------------------------------------
# Schema / validation
# ---------------------------------------------------------------------------


class TestSchema:
    def test_default_fields(self) -> None:
        entry = TelemetryEntry(path="sdk", tools_fired=["x"])
        assert entry.schema_version == SCHEMA_VERSION
        assert entry.path == "sdk"
        assert entry.tools_fired == ["x"]
        # timestamp should be parseable and roughly now.
        ts = datetime.fromisoformat(entry.timestamp)
        assert ts.tzinfo is not None

    def test_path_validation_rejects_unknown(self) -> None:
        with pytest.raises(ValueError):
            TelemetryEntry(path="bogus")

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            TelemetryEntry(path="sdk", duration_ms=-1)
        with pytest.raises(ValueError):
            TelemetryEntry(path="sdk", dispatch_ms=-1)

    def test_bad_schema_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            TelemetryEntry(schema_version=99, path="sdk")

    def test_unparseable_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError):
            TelemetryEntry(path="sdk", timestamp="not-a-date")

    def test_bad_json_line_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_line("{not json")


# ---------------------------------------------------------------------------
# Round-trip and read behavior
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    entry = _entry(path="schedule", tools_fired=["cal", "vault"], error="TimeoutError")
    append_entry(entry, data_path=data, lock_path=lock)
    result = read_recent(10, data_path=data, lock_path=lock)
    assert result == [
        {
            "schema_version": SCHEMA_VERSION,
            "timestamp": entry.timestamp,
            "path": "schedule",
            "duration_ms": 12.0,
            "stt_ms": None,
            "dispatch_ms": 7.0,
            "tools_fired": ["cal", "vault"],
            "error": "TimeoutError",
        }
    ]


def test_written_line_carrys_schema_version(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    append_entry(_entry(), data_path=data, lock_path=lock)
    line = data.read_text().strip()
    assert f'"schema_version":{SCHEMA_VERSION}' in line


def test_read_recent_missing_file_returns_empty(tmp_path: Path) -> None:
    data = tmp_path / "nope.jsonl"
    lock = tmp_path / "nope.jsonl.lock"
    assert read_recent(5, data_path=data, lock_path=lock) == []


def test_corrupt_final_line_tolerated(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    append_entry(_entry(path="sdk"), data_path=data, lock_path=lock)
    append_entry(_entry(path="schedule"), data_path=data, lock_path=lock)
    # Corrupt the final line: truncate mid-object.
    data.write_text(data.read_text()[:-3])
    result = read_recent(10, data_path=data, lock_path=lock)
    assert len(result) == 1
    assert result[0]["path"] == "sdk"


def test_read_recent_limit_returns_last_n(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    for i in range(5):
        append_entry(_entry(path="sdk", duration_ms=float(i)), data_path=data, lock_path=lock)
    result = read_recent(2, data_path=data, lock_path=lock)
    assert [e["duration_ms"] for e in result] == [3.0, 4.0]


def test_file_permissions(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    append_entry(_entry(), data_path=data, lock_path=lock)
    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Rotation (drop corrupt + expired + over-limit) — call _rotate_locked directly
# ---------------------------------------------------------------------------


def _mk(ts: str, path="sdk", tools=None) -> str:
    tools = tools if tools is not None else []
    return (
        '{"schema_version":1,"timestamp":"%s","path":"%s","duration_ms":1.0,'
        '"stt_ms":null,"dispatch_ms":1.0,"tools_fired":%s,"error":null}'
        % (ts, path, json.dumps(tools))
    )


def test_rotation_drops_invalid_and_old_survives_new(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"

    lines = [
        # corrupt (truncated, bad JSON) — must be dropped
        '{"schema_version":1,"path":"sdk"',
        # wrong schema version — must be dropped
        '{"schema_version":9,"timestamp":"%s","path":"sdk","duration_ms":1.0,'
        '"stt_ms":null,"dispatch_ms":1.0,"tools_fired":[],"error":null}' % _iso(0),
        # unparseable timestamp — must be dropped
        '{"schema_version":1,"timestamp":"garbage","path":"sdk","duration_ms":1.0,'
        '"stt_ms":null,"dispatch_ms":1.0,"tools_fired":[],"error":null}',
        # valid but >30 days old — must be dropped
        _mk(_iso(31 * 86400)),
        # valid and recent — must survive
        _mk(_iso(60), path="schedule", tools=["t"]),
    ]
    # Filler: valid, but all >30 days old, so rotation drops them regardless of
    # the most-recent-5000 cutoff. Sized to push the total just over the threshold.
    filler = [
        _mk((datetime.now(timezone.utc) - timedelta(seconds=60 * 86400)).isoformat())
        for _ in range(ROTATION_LINE_THRESHOLD + 1 - len(lines))
    ]
    data.write_text("\n".join(lines + filler) + "\n", encoding="utf-8")

    store._rotate_locked(data)
    remaining = [parse_line(l) for l in data.read_text().splitlines() if l.strip()]
    assert len(remaining) == 1
    assert remaining[0].path == "schedule"
    assert remaining[0].tools_fired == ["t"]


def test_rotation_drops_beyond_most_recent_5000(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"

    # Above the rotation threshold (5,500), all in-retention (60s old),
    # monotonically increasing timestamps; the most recent 5,000 must be the
    # ones that survive.
    total = ROTATION_LINE_THRESHOLD + 200  # 5,700
    lines = []
    for i in range(total):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=60) + timedelta(seconds=i)).isoformat()
        lines.append(_mk(ts))
    data.write_text("\n".join(lines) + "\n", encoding="utf-8")

    store._rotate_locked(data)
    remaining = [parse_line(l) for l in data.read_text().splitlines() if l.strip()]
    assert len(remaining) == 5000
    # The survivors must be the newest 5,000 by timestamp (the tail of the original),
    # in their original relative order.
    newest = [parse_line(l) for l in lines[-5000:]]
    assert [e.timestamp for e in remaining] == [e.timestamp for e in newest]


# ---------------------------------------------------------------------------
# Cross-process concurrency (real OS processes)
# ---------------------------------------------------------------------------


def _worker_append(data: str, lock: str, count: int) -> None:
    for _ in range(count):
        append_entry(
            TelemetryEntry(path="sdk", duration_ms=1.0),
            data_path=Path(data),
            lock_path=Path(lock),
        )


def _worker_read(data: str, lock: str, out_queue) -> None:
    for _ in range(3):
        # Must never raise json.JSONDecodeError from a torn read.
        read_recent(10, data_path=Path(data), lock_path=Path(lock))
        if out_queue is not None:
            out_queue.put("ok")


def test_concurrent_appends_no_lost_entries(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    per_process = 20
    n_proc = 4

    procs = [
        multiprocessing.Process(target=_worker_append, args=(str(data), str(lock), per_process))
        for _ in range(n_proc)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert all(p.exitcode == 0 for p in procs)

    result = read_recent(n_proc * per_process, data_path=data, lock_path=lock)
    assert len(result) == n_proc * per_process


def test_appender_and_reader_never_sees_torn_file(tmp_path: Path) -> None:
    data = tmp_path / "telemetry.jsonl"
    lock = tmp_path / "telemetry.jsonl.lock"
    # Seed the file above the rotation threshold so the very next append
    # triggers a rewrite, racing against the reader. Seeding lines are all
    # >30 days old so a rotation that fires drops them (content changes shape),
    # which is what exercises "pre- vs post-rotation" divergence.
    ts_old = (datetime.now(timezone.utc) - timedelta(seconds=90 * 86400)).isoformat()
    data.write_text(
        "\n".join([_mk(ts_old)] * (ROTATION_LINE_THRESHOLD + 1)) + "\n",
        encoding="utf-8",
    )

    q = multiprocessing.Queue()
    reader = multiprocessing.Process(target=_worker_read, args=(str(data), str(lock), q))
    appender = multiprocessing.Process(target=_worker_append, args=(str(data), str(lock), 3))
    reader.start()
    appender.start()
    appender.join()
    reader.join()
    assert reader.exitcode == 0, (
        "reader encountered a torn/partial read or corrupted JSON"
    )
