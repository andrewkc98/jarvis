import keyring
import json
from pathlib import Path

from jarvis import config


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
