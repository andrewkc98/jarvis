import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import keyring
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_PATH))

from jarvis import config


VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
VOICE_INPUT_MODE_ENV_VAR = "JARVIS_VOICE_INPUT_MODE"


def _reload_config_with_voice_input_mode(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(VOICE_INPUT_MODE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(VOICE_INPUT_MODE_ENV_VAR, value)
    return importlib.reload(config)


def _restore_default_voice_input_mode(monkeypatch):
    monkeypatch.delenv(VOICE_INPUT_MODE_ENV_VAR, raising=False)
    importlib.reload(config)


def _run_config_import_with_voice_input_mode(value):
    env = os.environ.copy()
    if value is None:
        env.pop(VOICE_INPUT_MODE_ENV_VAR, None)
    else:
        env[VOICE_INPUT_MODE_ENV_VAR] = value
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SRC_PATH)
        if not existing_pythonpath
        else os.pathsep.join((str(SRC_PATH), existing_pythonpath))
    )
    return subprocess.run(
        [
            str(VENV_PYTHON),
            "-c",
            "import jarvis.config as config; print(config.VOICE_INPUT_MODE)",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_voice_input_mode_defaults_to_off_when_absent(monkeypatch):
    try:
        loaded = _reload_config_with_voice_input_mode(monkeypatch, None)
        assert loaded.VOICE_INPUT_MODE == "off"
    finally:
        _restore_default_voice_input_mode(monkeypatch)


@pytest.mark.parametrize("mode", ["terminal", "browser", "off"])
def test_voice_input_mode_accepts_exact_values(monkeypatch, mode):
    try:
        loaded = _reload_config_with_voice_input_mode(monkeypatch, mode)
        assert loaded.VOICE_INPUT_MODE == mode
    finally:
        _restore_default_voice_input_mode(monkeypatch)


@pytest.mark.parametrize("mode", ["", "Browser", " terminal ", "banana"])
def test_voice_input_mode_rejects_invalid_values_without_echoing_value(
    monkeypatch, mode
):
    try:
        with pytest.raises(RuntimeError) as exc_info:
            _reload_config_with_voice_input_mode(monkeypatch, mode)
        message = str(exc_info.value)
        assert VOICE_INPUT_MODE_ENV_VAR in message
        if mode:
            assert mode not in message
    finally:
        _restore_default_voice_input_mode(monkeypatch)


def test_voice_input_mode_subprocess_defaults_to_off_when_absent(monkeypatch):
    monkeypatch.delenv(VOICE_INPUT_MODE_ENV_VAR, raising=False)
    try:
        result = _run_config_import_with_voice_input_mode(None)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "off"
    finally:
        _restore_default_voice_input_mode(monkeypatch)


@pytest.mark.parametrize("mode", ["terminal", "browser", "off"])
def test_voice_input_mode_subprocess_accepts_exact_values(monkeypatch, mode):
    monkeypatch.delenv(VOICE_INPUT_MODE_ENV_VAR, raising=False)
    try:
        result = _run_config_import_with_voice_input_mode(mode)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == mode
    finally:
        _restore_default_voice_input_mode(monkeypatch)


@pytest.mark.parametrize("mode", ["", "Browser", " terminal ", "banana"])
def test_voice_input_mode_subprocess_rejects_invalid_values_without_echoing_value(
    monkeypatch, mode
):
    monkeypatch.delenv(VOICE_INPUT_MODE_ENV_VAR, raising=False)
    try:
        result = _run_config_import_with_voice_input_mode(mode)
        assert result.returncode != 0
        assert VOICE_INPUT_MODE_ENV_VAR in result.stderr
        if mode:
            assert mode not in result.stderr
    finally:
        _restore_default_voice_input_mode(monkeypatch)


def test_get_credential_from_environment(monkeypatch):
    monkeypatch.setenv("TEST_CREDENTIAL", "from-env")
    assert config.get_credential("TEST_CREDENTIAL") == "from-env"


def test_get_credential_from_keyring(monkeypatch):
    monkeypatch.delenv("TEST_CREDENTIAL", raising=False)
    monkeypatch.setattr(keyring, "get_password", lambda service, name: "from-keyring")
    assert config.get_credential("TEST_CREDENTIAL") == "from-keyring"


def test_get_credential_missing(monkeypatch):
    monkeypatch.delenv("TEST_CREDENTIAL", raising=False)
    monkeypatch.setattr(keyring, "get_password", lambda service, name: None)
    try:
        config.get_credential("TEST_CREDENTIAL")
    except RuntimeError as exc:
        assert "TEST_CREDENTIAL" in str(exc)
    else:
        raise AssertionError("missing credential should raise RuntimeError")


def test_generate_mcp_config_substitutes_and_secures_file(tmp_path, monkeypatch):
    template_path = tmp_path / "template.json"
    output_path = tmp_path / "output.json"
    template_path.write_text('{"token": "${TEST_VAR}"}')
    monkeypatch.setattr(config, "get_credential", lambda name: "known-value")

    config.generate_mcp_config(template_path, output_path)

    assert output_path.exists()
    assert "known-value" in output_path.read_text()
    assert "${TEST_VAR}" not in output_path.read_text()
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_generate_mcp_config_renders_real_native_obsidian_template(tmp_path, monkeypatch):
    template_path = Path(__file__).resolve().parents[1] / "mcp-config.json.example"
    output_path = tmp_path / "mcp-config.json"
    fake_token = "fake-test-token-value"
    monkeypatch.setattr(config, "get_credential", lambda name: fake_token)

    config.generate_mcp_config(template_path, output_path)

    rendered_text = output_path.read_text()
    rendered = json.loads(rendered_text)
    obsidian = rendered["mcpServers"]["obsidian"]
    assert obsidian["type"] == "http"
    assert obsidian["url"] == "http://127.0.0.1:27123/mcp/"
    assert obsidian["headers"]["Authorization"] == "Bearer fake-test-token-value"
    assert "${OBSIDIAN_REST_TOKEN}" not in rendered_text


@pytest.mark.parametrize("existing_destination", [False, True])
def test_generate_mcp_config_publishes_complete_content_atomically(
    tmp_path, monkeypatch, existing_destination
):
    template_path = tmp_path / "template.json"
    output_path = tmp_path / "output.json"
    template_path.write_text('{"token": "${TEST_CREDENTIAL}"}')
    if existing_destination:
        output_path.write_text('{"token": "old-value"}')
        os.chmod(output_path, 0o640)

    observed_source_modes = []
    real_replace = os.replace

    def capture_source_mode(source, destination):
        observed_source_modes.append(Path(source).stat().st_mode & 0o777)
        return real_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", capture_source_mode)
    monkeypatch.setattr(config, "get_credential", lambda name: "fake-published-value")

    config.generate_mcp_config(template_path, output_path)

    assert observed_source_modes == [0o600]
    assert output_path.read_text() == '{"token": "fake-published-value"}'
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_generate_mcp_config_failure_preserves_destination_and_cleans_failed_temp(
    tmp_path, monkeypatch
):
    template_path = tmp_path / "template.json"
    output_path = tmp_path / "output.json"
    unrelated_temp_path = tmp_path / "output.json.unrelated.tmp"
    template_path.write_text('{"token": "${TEST_CREDENTIAL}"}')
    output_path.write_text('{"token": "old-value"}')
    os.chmod(output_path, 0o640)
    unrelated_temp_path.write_text("keep this file")

    failed_temp_paths = []

    def fail_before_replace(source, destination):
        failed_temp_paths.append(Path(source))
        assert Path(source).stat().st_mode & 0o777 == 0o600
        raise OSError("injected publication failure")

    monkeypatch.setattr(config.os, "replace", fail_before_replace)
    monkeypatch.setattr(config, "get_credential", lambda name: "fake-failed-value")

    with pytest.raises(OSError, match="injected publication failure"):
        config.generate_mcp_config(template_path, output_path)

    assert len(failed_temp_paths) == 1
    assert not failed_temp_paths[0].exists()
    assert output_path.read_text() == '{"token": "old-value"}'
    assert output_path.stat().st_mode & 0o777 == 0o640
    assert template_path.exists()
    assert unrelated_temp_path.read_text() == "keep this file"


def test_generate_mcp_config_concurrent_publications_are_complete_and_secure(
    tmp_path, monkeypatch
):
    template_path = tmp_path / "template.json"
    output_path = tmp_path / "output.json"
    template_path.write_text('{"token": "${TEST_CREDENTIAL}"}')

    replace_barrier = threading.Barrier(2)
    temporary_paths = []
    temporary_paths_lock = threading.Lock()
    real_replace = os.replace

    def synchronize_replacement(source, destination):
        with temporary_paths_lock:
            temporary_paths.append(Path(source))
        replace_barrier.wait()
        return real_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", synchronize_replacement)
    credential_by_writer = {
        "config-writer-a": "fake-concurrent-value-a",
        "config-writer-b": "fake-concurrent-value-b",
    }
    monkeypatch.setattr(
        config,
        "get_credential",
        lambda name: credential_by_writer[threading.current_thread().name],
    )

    errors = []
    errors_lock = threading.Lock()

    def publish_from_thread():
        try:
            config.generate_mcp_config(template_path, output_path)
        except BaseException as exc:  # pragma: no cover - assertion below reports failures
            with errors_lock:
                errors.append(exc)

    writers = [
        threading.Thread(target=publish_from_thread, name="config-writer-a"),
        threading.Thread(target=publish_from_thread, name="config-writer-b"),
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    assert errors == []
    assert len(temporary_paths) == 2
    assert all(not temporary_path.exists() for temporary_path in temporary_paths)
    assert output_path.read_text() in {
        '{"token": "fake-concurrent-value-a"}',
        '{"token": "fake-concurrent-value-b"}',
    }
    assert output_path.stat().st_mode & 0o777 == 0o600
    assert {path.name for path in tmp_path.iterdir()} == {"template.json", "output.json"}
