"""Contract tests for the HUD's production schedule formatter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest


DIM_TIME = "#aeb5b2"
DIM_REL = "#5d6663"
SOON = "#4fe0cf"


def _run_formatter(fixtures, tmp_path):
    source = (os.path.dirname(os.path.dirname(__file__)) + "/hud/index.html")
    html = open(source, encoding="utf-8").read()
    start_marker = "// schedule-format:start"
    end_marker = "// schedule-format:end"
    assert html.count(start_marker) == 1
    assert html.count(end_marker) == 1
    start = html.index(start_marker)
    end = html.index(end_marker)
    assert start < end
    formatter = html[start:end]
    script = tmp_path / "hud_schedule_contract.js"
    script.write_text(
        formatter
        + "\n"
        + "const fs = require('fs');\n"
        + "const fixtures = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        + "const result = fixtures.map(({event, now}) => formatScheduleEvent(event, new Date(now)));\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js binary not found; HUD contract test requires node")
    env = os.environ.copy()
    env["TZ"] = "Europe/Berlin"
    try:
        completed = subprocess.run(
            [node, str(script)],
            input=json.dumps(fixtures),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        pytest.fail(f"could not invoke Node.js: {exc}")
    if completed.returncode != 0:
        pytest.fail(
            f"HUD formatter Node process failed with exit code {completed.returncode}:\n"
            f"{completed.stderr}"
        )
    return json.loads(completed.stdout)


def _expected(time, dur, rel, soon=False, *, dim=True):
    return {
        "time": time,
        "dur": dur,
        "rel": rel,
        "soon": soon,
        "timeColor": DIM_TIME if dim else SOON,
        "relColor": DIM_REL if dim else SOON,
    }


def test_schedule_formatter_contract(tmp_path):
    fixtures = [
        # All-day state and relative labels.
        {
            "event": {"all_day": True, "start": "2026-09-14", "end": "2026-09-15"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        {
            "event": {"all_day": True, "start": "2026-09-10", "end": "2026-09-29"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        {
            "event": {"all_day": True, "start": "2026-09-14", "end": "2026-09-28"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        {
            "event": {"all_day": True, "start": "2026-09-15", "end": "2026-09-16"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        {
            "event": {"all_day": True, "start": "2026-09-19", "end": "2026-09-20"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        {
            "event": {"all_day": True, "start": "2026-09-24", "end": "2026-09-25"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        # Calendar-day duration remains one day across DST transitions.
        {
            "event": {"all_day": True, "start": "2026-03-29", "end": "2026-03-30"},
            "now": "2026-03-29T12:00:00+02:00",
        },
        {
            "event": {"all_day": True, "start": "2026-10-25", "end": "2026-10-26"},
            "now": "2026-10-25T12:00:00+01:00",
        },
        # Timed events retain clock time and use hour/day/week duration buckets.
        {
            "event": {"start": "2026-09-14T00:00:00+02:00", "end": "2026-09-14T01:30:00+02:00"},
            "now": "2026-09-13T23:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-10T08:00:00+02:00", "end": "2026-09-12T11:00:00+02:00"},
            "now": "2026-09-09T12:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-01T08:00:00+02:00", "end": "2026-09-08T10:00:00+02:00"},
            "now": "2026-08-31T12:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-14T11:00:00+02:00", "end": "2026-09-14T13:00:00+02:00"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-14T11:56:00+02:00", "end": "2026-09-14T11:59:00+02:00"},
            "now": "2026-09-14T12:00:00+02:00",
        },
        # Timed duration rounds once before crossing minute/hour/day/week buckets.
        {
            "event": {"start": "2026-09-14T00:00:00+02:00", "end": "2026-09-14T00:59:36+02:00"},
            "now": "2026-09-13T23:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-14T00:00:00+02:00", "end": "2026-09-14T01:59:36+02:00"},
            "now": "2026-09-13T23:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-14T00:00:00+02:00", "end": "2026-09-14T23:59:36+02:00"},
            "now": "2026-09-13T23:00:00+02:00",
        },
        {
            "event": {"start": "2026-09-14T00:00:00+02:00", "end": "2026-09-20T23:59:36+02:00"},
            "now": "2026-09-13T23:00:00+02:00",
        },
        # Invalid and empty intervals use safe labels and dim styling.
        {"event": {"start": "not-a-date", "end": "2026-09-14T12:30:00+02:00"}, "now": "2026-09-14T12:00:00+02:00"},
        {"event": {"start": "2026-09-14T12:00:00+02:00"}, "now": "2026-09-14T12:00:00+02:00"},
        {"event": {"start": "2026-09-14T12:00:00+02:00", "end": "bad"}, "now": "2026-09-14T12:00:00+02:00"},
        {"event": {"start": "2026-09-14T12:00:00+02:00", "end": "2026-09-14T12:00:00+02:00"}, "now": "2026-09-14T12:00:00+02:00"},
        {"event": {"start": "2026-09-14T13:00:00+02:00", "end": "2026-09-14T12:00:00+02:00"}, "now": "2026-09-14T12:00:00+02:00"},
    ]
    actual = _run_formatter(fixtures, tmp_path)
    expected = [
        _expected("all day", "1 day", "today"),
        _expected("all day", "2w 5d", "in progress"),
        _expected("all day", "2 weeks", "in progress"),
        _expected("all day", "1 day", "tomorrow"),
        _expected("all day", "1 day", "in 5 days"),
        _expected("all day", "1 day", "in 1w 3d"),
        _expected("all day", "1 day", "today"),
        _expected("all day", "1 day", "today"),
        _expected("00:00", "1h 30m", "in 1h 0m"),
        _expected("08:00", "2d 3h", "in 20h 0m"),
        _expected("08:00", "1w", "in 20h 0m"),
        _expected("11:00", "2h", "in progress"),
        _expected("11:56", "3 min", "ended"),
        _expected("00:00", "1h", "in 1h 0m"),
        _expected("00:00", "2h", "in 1h 0m"),
        _expected("00:00", "1 day", "in 1h 0m"),
        _expected("00:00", "1 week", "in 1h 0m"),
        _expected("--:--", "", ""),
        _expected("12:00", "", ""),
        _expected("12:00", "", ""),
        _expected("12:00", "", ""),
        _expected("13:00", "", ""),
    ]
    assert actual == expected
    assert actual[3]["soon"] is False
    assert actual[12]["soon"] is False
