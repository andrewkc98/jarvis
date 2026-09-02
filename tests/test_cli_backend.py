import json
import subprocess
from pathlib import Path

import pytest

from jarvis import config
from jarvis.orchestrator import cli_backend


FIXTURE = Path(__file__).parent / "fixtures" / "claude_list_mcp_tools.json"


def _completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout, stderr)


@pytest.fixture
def mocked_config(monkeypatch, tmp_path):
    output = tmp_path / "mcp-config.json"
    monkeypatch.setattr(config, "MCP_CONFIG_PATH", output)
    monkeypatch.setattr(cli_backend, "_MCP_TEMPLATE_PATH", tmp_path / "template.json")
    cli_backend._MCP_TEMPLATE_PATH.write_text('{"mcpServers": {}}')
    monkeypatch.setattr(config, "generate_mcp_config", lambda template, destination: destination.write_text("{}"))


def test_success_uses_real_schema_fixture(monkeypatch, mocked_config):
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed('{"result":"synthetic success"}'))
    assert cli_backend.ask("list files") == "synthetic success"


def test_nonzero_exit_includes_observed_json_result(monkeypatch, mocked_config):
    observed = FIXTURE.read_text()
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed(observed, 1))
    with pytest.raises(RuntimeError, match="Not logged in"):
        cli_backend.ask("list files")


def test_mcp_errors_raise(monkeypatch, mocked_config):
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed(json.dumps({"result": "partial", "mcp_server_errors": ["offline"]})))
    with pytest.raises(RuntimeError, match="offline"):
        cli_backend.ask("query")


def test_timeout_raises(monkeypatch, mocked_config):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(cli_backend.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        cli_backend.ask("query")


def test_nonzero_exit_includes_stderr(monkeypatch, mocked_config):
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed("{}", 2, "bad config"))
    with pytest.raises(RuntimeError, match="bad config"):
        cli_backend.ask("query")


def test_malformed_json_raises(monkeypatch, mocked_config):
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed("not json"))
    with pytest.raises(RuntimeError, match="malformed JSON"):
        cli_backend.ask("query")


@pytest.mark.parametrize("stdout", ["", "{}"])
def test_empty_or_missing_stdout_raises(monkeypatch, mocked_config, stdout):
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed(stdout))
    with pytest.raises(RuntimeError):
        cli_backend.ask("query")


def test_error_marker_from_observed_schema_raises(monkeypatch, mocked_config):
    payload = json.loads(FIXTURE.read_text())
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: _completed(json.dumps(payload)))
    with pytest.raises(RuntimeError, match="Not logged in"):
        cli_backend.ask("query")


def test_argv_and_generation(monkeypatch, mocked_config):
    calls = []
    monkeypatch.setattr(cli_backend.subprocess, "run", lambda *args, **kwargs: (calls.append((args, kwargs)) or _completed('{"result":"ok"}')))
    assert cli_backend.ask("hello") == "ok"
    argv, kwargs = calls[0]
    assert argv[0] == [
        "claude",
        "-p",
        "hello",
        "--mcp-config",
        str(config.MCP_CONFIG_PATH),
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
    ]
    for flag in ("--strict-mcp-config", "--tools", "--allowedTools", "--setting-sources"):
        assert flag not in argv[0]
    assert kwargs["shell"] is not True if "shell" in kwargs else True


def test_argv_uses_scratch_working_directory(monkeypatch, mocked_config):
    calls = []
    monkeypatch.setattr(
        cli_backend.subprocess,
        "run",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or _completed('{"result":"ok"}')),
    )

    cli_backend.ask("hello")

    _, kwargs = calls[0]
    assert kwargs["cwd"] == str(cli_backend._SCRATCH_CWD)
