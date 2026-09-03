"""Command-line entry point for a single Jarvis interaction."""

import argparse
import asyncio

from jarvis.orchestrator.service import JarvisService


async def _amain(args) -> str:
    service = JarvisService(voice_model_path=args.voice_model, runtime_source="cli")
    try:
        if args.text is not None:
            return await service.arun_text(args.text)
        return await service.arun_once()
    finally:
        await service.aclose()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run one Jarvis interaction.")
    parser.add_argument(
        "--text",
        help="Use this text directly, skipping audio capture and transcription.",
    )
    parser.add_argument(
        "--voice-model",
        required=True,
        help="Path to the Piper .onnx voice model.",
    )
    args = parser.parse_args(argv)

    response = asyncio.run(_amain(args))
    print(response)


if __name__ == "__main__":
    main()
