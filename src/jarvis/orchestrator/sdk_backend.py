"""Async Claude Agent SDK backend for open-ended vault queries with full
tool/MCP capability (accepted-risk bypassPermissions posture; see
docs/threat-model.md). orchestrator/cli_backend.py is Phase 1b's predecessor and is
reference-only — nothing here calls it, and this module is not called by it.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent

from jarvis import config


_MCP_TEMPLATE_PATH = config.MCP_CONFIG_PATH.with_name("mcp-config.json.example")
_SCRATCH_CWD = Path(tempfile.gettempdir()) / "jarvis-sdk-scratch"
_MAX_DIAGNOSTIC_LENGTH = 2000


def _bounded_diagnostic(value: str) -> str:
    """Keep SDK diagnostics useful without echoing an unbounded response."""
    if len(value) <= _MAX_DIAGNOSTIC_LENGTH:
        return value
    return f"{value[:_MAX_DIAGNOSTIC_LENGTH]}..."


class McpServerUnavailableError(RuntimeError):
    """Raised when a configured MCP server is not usable right after connecting."""


class SDKBackend:
    """Owns one lazily-connected, reused ClaudeSDKClient for the process lifetime."""

    def __init__(self) -> None:
        self._client: ClaudeSDKClient | None = None

    async def _ensure_connected(self) -> ClaudeSDKClient:
        if self._client is not None:
            return self._client
        _SCRATCH_CWD.mkdir(parents=True, exist_ok=True)
        config.generate_mcp_config(_MCP_TEMPLATE_PATH, config.MCP_CONFIG_PATH)
        options = ClaudeAgentOptions(
            mcp_servers=config.MCP_CONFIG_PATH,
            permission_mode="bypassPermissions",
            cwd=str(_SCRATCH_CWD),
            include_partial_messages=True,
        )
        client = ClaudeSDKClient(options)
        await client.connect()
        try:
            await self._check_mcp_health(client)
        except BaseException:
            await client.disconnect()
            raise
        self._client = client
        return client

    @staticmethod
    async def _check_mcp_health(client: ClaudeSDKClient) -> None:
        status = await client.get_mcp_status()
        for server in status["mcpServers"]:
            if server["status"] in ("failed", "needs-auth"):
                detail = server.get("error") or "no error detail"
                raise McpServerUnavailableError(
                    f"MCP server {server['name']!r} is {server['status']}: "
                    f"{_bounded_diagnostic(str(detail))}"
                )

    async def close(self) -> None:
        """Idempotent: a no-op if never connected."""
        if self._client is None:
            return
        client, self._client = self._client, None
        await client.disconnect()

    async def ask_stream(self, prompt: str) -> AsyncIterator[str]:
        """Ask Claude a vault question, yielding streamed text deltas as they arrive."""
        client = await self._ensure_connected()
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                if message.parent_tool_use_id is not None:
                    continue
                event = message.event
                if event.get("type") != "content_block_delta":
                    continue
                delta = event.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue
                text = delta.get("text")
                if text:
                    yield text
            elif isinstance(message, ResultMessage):
                if message.is_error:
                    detail = message.result or "; ".join(message.errors or []) or "unknown error"
                    raise RuntimeError(
                        f"Claude SDK reported an error: {_bounded_diagnostic(str(detail))}"
                    )
