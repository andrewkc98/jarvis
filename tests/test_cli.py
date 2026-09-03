import asyncio
from argparse import Namespace

from jarvis.orchestrator import cli


def test_amain_constructs_cli_runtime_source(monkeypatch):
    calls = []

    class FakeService:
        def __init__(self, *, voice_model_path, runtime_source):
            calls.append((voice_model_path, runtime_source))

        async def arun_text(self, text):
            return f"answer:{text}"

        async def aclose(self):
            calls.append("closed")

    monkeypatch.setattr(cli, "JarvisService", FakeService)

    result = asyncio.run(
        cli._amain(Namespace(text="hello", voice_model="voice.onnx"))
    )

    assert result == "answer:hello"
    assert calls == [("voice.onnx", "cli"), "closed"]
