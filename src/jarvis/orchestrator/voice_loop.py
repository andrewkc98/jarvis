"""Reusable interactive voice loop for a single terminal process.

This module starts one :class:`~jarvis.orchestrator.service.JarvisService` and
spins it repeatedly so the user can keep conversing until they stop it. It is
intended to be launched as ``python -m jarvis.orchestrator.voice_loop`` by a
parent launcher, which keeps the API and HUD processes free to own other input
sources.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Callable

from jarvis.orchestrator.service import JarvisService


async def arun_loop(
    service: JarvisService, *, out: Callable[[str], object] = print
) -> None:
    """Run voice turns until the service signals a clean end.

    Await :meth:`service.arun_once` on every iteration and forward each
    successful response to ``out``. :class:`EOFError` ends the loop cleanly;
    any other :class:`Exception` emits one concise single-line summary (with the
    exception type, line breaks flattened) and keeps the loop going.
    On return, on a requested stop, or on any propagating exception, the service
    is closed exactly once.
    """
    try:
        while True:
            try:
                response = await service.arun_once()
            except EOFError:
                # Clean terminal close: stop without retrying or reporting a turn.
                break
            except (KeyboardInterrupt, asyncio.CancelledError):
                # Requested stop / cancellation: leave immediately, no failed-turn note.
                raise
            except Exception as exc:
                # Recoverable: one concise single-line message, then keep going.
                message = "voice loop error: {}:".format(type(exc).__name__)
                detail = " ".join(
                    line.strip()
                    for line in str(exc).splitlines()
                    if line.strip()
                )
                if detail:
                    message += " {}".format(detail)
                out(message)
                continue
            out(response)
    finally:
        # Close exactly once, even if this coroutine's task is being cancelled as
        # part of the requested stop; shield lets aclose finish uninterrupted.
        await asyncio.shield(service.aclose())


def main(argv: list[str] | None = None) -> int:
    """Entry point: construct one service and run the loop under asyncio.

    Installs a SIGTERM handler that raises ``KeyboardInterrupt`` so a blocking
    terminal capture can be interrupted, then restores the previous handler.
    Clean ends (EOF, ``KeyboardInterrupt``, cancellation) exit ``0``; any other
    unhandled failure exits nonzero.
    """
    parser = argparse.ArgumentParser(
        description="Run an interactive Jarvis voice loop in this terminal."
    )
    parser.add_argument(
        "--voice-model",
        required=True,
        help="Path to the Piper .onnx voice model.",
    )
    args = parser.parse_args(argv)

    previous_term = signal.getsignal(signal.SIGTERM)

    def _handle_term(signum, frame):  # pragma: no cover - signal wiring
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle_term)

    try:
        try:
            service = JarvisService(
                voice_model_path=args.voice_model, runtime_source="cli"
            )
            asyncio.run(arun_loop(service))
            return 0
        except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
            # EOF-equivalent, requested stop, or cancellation: clean status.
            return 0
    except Exception:
        # Any other unhandled failure is non-zero.
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
