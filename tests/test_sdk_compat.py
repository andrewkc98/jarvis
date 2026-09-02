"""Guards Phase 2's design assumptions against a future claude-agent-sdk upgrade.

If any assertion here fails after a version bump, the SDK's shape changed in a way
sdk_backend.py depends on — fix sdk_backend.py (and this file) before touching anything
else, don't silence the failure.
"""

import dataclasses
import inspect

import claude_agent_sdk as sdk


def test_installed_version():
    assert sdk.__version__ == "0.2.151"


def test_claude_agent_options_has_required_fields():
    fields = {f.name: f for f in dataclasses.fields(sdk.ClaudeAgentOptions)}
    for name in (
        "mcp_servers",
        "permission_mode",
        "cwd",
        "include_partial_messages",
        "strict_mcp_config",
        "allowed_tools",
        "disallowed_tools",
    ):
        assert name in fields, f"ClaudeAgentOptions is missing {name!r}"
    assert fields["include_partial_messages"].default is False
    assert fields["strict_mcp_config"].default is False
    assert fields["cwd"].default is None


def test_claude_sdk_client_has_required_async_methods():
    for name in ("connect", "query", "disconnect", "get_mcp_status"):
        member = getattr(sdk.ClaudeSDKClient, name)
        assert inspect.iscoroutinefunction(member), f"{name} is not async"
    assert inspect.isasyncgenfunction(sdk.ClaudeSDKClient.receive_response)


def test_stream_event_shape():
    fields = {f.name for f in dataclasses.fields(sdk.StreamEvent)}
    assert {"uuid", "session_id", "event", "parent_tool_use_id"} <= fields


def test_result_message_shape():
    fields = {f.name for f in dataclasses.fields(sdk.ResultMessage)}
    assert {
        "subtype",
        "duration_ms",
        "duration_api_ms",
        "is_error",
        "num_turns",
        "session_id",
        "result",
        "errors",
        "api_error_status",
    } <= fields


def test_exception_hierarchy():
    assert issubclass(sdk.CLIConnectionError, sdk.ClaudeSDKError)
    assert issubclass(sdk.CLINotFoundError, sdk.CLIConnectionError)
    assert issubclass(sdk.ProcessError, sdk.ClaudeSDKError)
    assert issubclass(sdk.ResultError, sdk.ProcessError)
    assert issubclass(sdk.CLIJSONDecodeError, sdk.ClaudeSDKError)
