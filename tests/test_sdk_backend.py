import asyncio

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent

from jarvis import config
from jarvis.orchestrator import sdk_backend


class FakeClient:
    """Stands in for claude_agent_sdk.ClaudeSDKClient in these tests."""

    instances: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.query_calls = []
        self.responses = []
        self.mcp_status = {"mcpServers": [{"name": "obsidian", "status": "connected"}]}
        FakeClient.instances.append(self)

    async def connect(self, prompt=None):
        self.connect_calls += 1

    async def disconnect(self):
        self.disconnect_calls += 1

    async def get_mcp_status(self):
        return self.mcp_status

    async def query(self, prompt, session_id="default"):
        self.query_calls.append(prompt)

    async def receive_response(self):
        for message in self.responses:
            yield message


@pytest.fixture(autouse=True)
def _reset_fake_client():
    FakeClient.instances = []
    yield


@pytest.fixture
def mocked_config(monkeypatch, tmp_path):
    output = tmp_path / "mcp-config.json"
    monkeypatch.setattr(config, "MCP_CONFIG_PATH", output)
    monkeypatch.setattr(sdk_backend, "_MCP_TEMPLATE_PATH", tmp_path / "template.json")
    sdk_backend._MCP_TEMPLATE_PATH.write_text('{"mcpServers": {}}')
    monkeypatch.setattr(
        config, "generate_mcp_config", lambda template, destination: destination.write_text("{}")
    )


@pytest.fixture
def mocked_client(monkeypatch):
    monkeypatch.setattr(sdk_backend, "ClaudeSDKClient", FakeClient)


def test_ensure_connected_connects_once_and_reuses_the_client(mocked_config, mocked_client):
    backend = sdk_backend.SDKBackend()
    first = asyncio.run(backend._ensure_connected())
    second = asyncio.run(backend._ensure_connected())
    assert first is second
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].connect_calls == 1


def test_ensure_connected_raises_on_failed_mcp_server(monkeypatch, mocked_config):
    class FailingClient(FakeClient):
        async def get_mcp_status(self):
            return {"mcpServers": [{"name": "obsidian", "status": "failed", "error": "boom"}]}

    monkeypatch.setattr(sdk_backend, "ClaudeSDKClient", FailingClient)
    backend = sdk_backend.SDKBackend()
    with pytest.raises(sdk_backend.McpServerUnavailableError, match="boom"):
        asyncio.run(backend._ensure_connected())


def test_ensure_connected_raises_on_needs_auth_mcp_server(monkeypatch, mocked_config):
    class NeedsAuthClient(FakeClient):
        async def get_mcp_status(self):
            return {"mcpServers": [{"name": "obsidian", "status": "needs-auth"}]}

    monkeypatch.setattr(sdk_backend, "ClaudeSDKClient", NeedsAuthClient)
    backend = sdk_backend.SDKBackend()
    with pytest.raises(sdk_backend.McpServerUnavailableError, match="needs-auth"):
        asyncio.run(backend._ensure_connected())


def test_close_is_idempotent_and_disconnects_once(mocked_config, mocked_client):
    backend = sdk_backend.SDKBackend()
    asyncio.run(backend._ensure_connected())
    asyncio.run(backend.close())
    asyncio.run(backend.close())
    assert FakeClient.instances[0].disconnect_calls == 1


def test_close_before_connect_is_a_no_op(mocked_config, mocked_client):
    backend = sdk_backend.SDKBackend()
    asyncio.run(backend.close())
    assert FakeClient.instances == []


def _stream_event(text, parent_tool_use_id=None):
    return StreamEvent(
        uuid="evt",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        parent_tool_use_id=parent_tool_use_id,
    )


def _result_message(is_error=False, result=None, errors=None):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        result=result,
        errors=errors,
    )


def test_ask_stream_yields_text_deltas_and_ignores_subagent_events(mocked_config, mocked_client):
    backend = sdk_backend.SDKBackend()

    async def run():
        client = await backend._ensure_connected()
        client.responses = [
            _stream_event("Hello "),
            _stream_event("ignored", parent_tool_use_id="subagent-1"),
            _stream_event("world."),
            _result_message(),
        ]
        return [chunk async for chunk in backend.ask_stream("hi")]

    assert asyncio.run(run()) == ["Hello ", "world."]
    assert FakeClient.instances[0].query_calls == ["hi"]


def test_ask_stream_raises_on_result_error(mocked_config, mocked_client):
    backend = sdk_backend.SDKBackend()

    async def run():
        client = await backend._ensure_connected()
        client.responses = [_stream_event("partial"), _result_message(is_error=True, result="boom")]
        return [chunk async for chunk in backend.ask_stream("hi")]

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())


def test_ask_stream_reuses_connection_across_two_calls(mocked_config, mocked_client):
    backend = sdk_backend.SDKBackend()

    async def run():
        client = await backend._ensure_connected()
        client.responses = [_stream_event("first"), _result_message()]
        first = [chunk async for chunk in backend.ask_stream("one")]
        client.responses = [_stream_event("second"), _result_message()]
        second = [chunk async for chunk in backend.ask_stream("two")]
        return first, second

    first, second = asyncio.run(run())
    assert first == ["first"]
    assert second == ["second"]
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].connect_calls == 1
    assert FakeClient.instances[0].query_calls == ["one", "two"]
