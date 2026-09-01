"""Command-line entry point for a single Jarvis interaction."""

import argparse

from jarvis.orchestrator.service import JarvisService


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

    service = JarvisService(voice_model_path=args.voice_model)
    if args.text is not None:
        response = service.run_text(args.text)
    else:
        response = service.run_once()
    print(response)


if __name__ == "__main__":
    main()
