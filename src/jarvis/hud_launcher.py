"""Supervised one-command launcher for the local JARVIS HUD."""

from __future__ import annotations

import argparse
import functools
import http.server
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser


API_PORT = 8765
DEFAULT_HUD_PORT = 4173
READINESS_TIMEOUT = 10.0
SHUTDOWN_TIMEOUT = 5.0
JOIN_TIMEOUT = 5.0


class _HudHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


class _Resources:
    """Owned resources and supervisor state for one launcher attempt."""

    def __init__(self) -> None:
        self.server: _HudHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.server_thread_started = False
        self.api_process: subprocess.Popen[object] | None = None
        self.voice_process: subprocess.Popen[object] | None = None
        self.static_error: BaseException | None = None
        self.static_done = threading.Event()
        self.user_stop = threading.Event()
        self.wakeup = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._cleaned = False

    def record_static_failure(self, error: BaseException) -> None:
        self.static_error = error
        self.static_done.set()
        self.wakeup.set()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_readable_regular_file(path: Path) -> bool:
    """Return whether *path* is a readable regular file.

    This deliberately remains a small, separately named predicate: tests can replace
    it without depending on the permissions of the account running the suite.
    """

    try:
        mode = path.stat().st_mode
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(mode) and os.access(path, os.R_OK)


def _absolute_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve(strict=False)


def _resolve_voice_model(option: str | None, environ: dict[str, str], root: Path) -> Path:
    if option is not None:
        if option == "":
            raise ValueError("--voice-model requires a non-empty path")
        raw = option
    elif "JARVIS_VOICE_MODEL" in environ:
        raw = environ["JARVIS_VOICE_MODEL"]
    else:
        raw = str(root / "en_US-lessac-medium.onnx")
    if raw == "":
        raise ValueError("JARVIS_VOICE_MODEL requires a non-empty path")
    return _absolute_path(raw)


_DECIMAL_INTEGER = re.compile(r"[0-9]+\Z")


def _strict_port(raw: str) -> int | None:
    if not isinstance(raw, str) or not _DECIMAL_INTEGER.fullmatch(raw):
        return None
    value = int(raw)
    if not 1 <= value <= 65535:
        return None
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace | None:
    parser = argparse.ArgumentParser(prog="jarvis-hud")
    parser.add_argument("--voice-model")
    parser.add_argument("--hud-port", default=str(DEFAULT_HUD_PORT))
    parser.add_argument("--no-browser", action="store_true")
    voice_group = parser.add_mutually_exclusive_group()
    voice_group.add_argument(
        "--browser-voice",
        action="store_true",
        help="use browser microphone input instead of terminal voice capture",
    )
    voice_group.add_argument("--no-voice", action="store_true", help=argparse.SUPPRESS)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        raise
    hud_port = _strict_port(args.hud_port)
    if hud_port is None:
        print("jarvis-hud: HUD port must be an integer from 1 to 65535", file=sys.stderr)
        return None
    if hud_port == API_PORT:
        print("jarvis-hud: HUD port must differ from the API port", file=sys.stderr)
        return None
    args.hud_port = hud_port
    args.mode = _resolve_voice_input_mode(args)
    return args


def _resolve_voice_input_mode(args: argparse.Namespace) -> str:
    if getattr(args, "browser_voice", False):
        return "browser"
    if getattr(args, "no_voice", False):
        return "off"
    mode = getattr(args, "mode", None)
    if mode in {"terminal", "browser", "off"}:
        return mode
    return "terminal"


def _validate_inherited_api_port(environ: dict[str, str]) -> bool:
    if "JARVIS_API_PORT" not in environ:
        return True
    raw = environ["JARVIS_API_PORT"]
    return _strict_port(raw) == API_PORT


def _validate_preflight(args: argparse.Namespace, environ: dict[str, str], root: Path) -> Path | None:
    try:
        model = _resolve_voice_model(args.voice_model, environ, root)
    except (OSError, ValueError):
        print("jarvis-hud: invalid voice-model path", file=sys.stderr)
        return None
    companion = Path(str(model) + ".json")
    if not _is_readable_regular_file(model):
        print("jarvis-hud: voice model is not a readable regular file", file=sys.stderr)
        return None
    if not _is_readable_regular_file(companion):
        print("jarvis-hud: voice model companion is not a readable regular file", file=sys.stderr)
        return None
    if not _validate_inherited_api_port(environ):
        print("jarvis-hud: inherited API port must be 8765", file=sys.stderr)
        return None
    return model


def _make_handler(root: Path) -> type[http.server.SimpleHTTPRequestHandler]:
    class HudHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

    return HudHandler


def _probe_url(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            return 200 <= int(status) < 300
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return False


def _failure(resources: _Resources) -> bool:
    if resources.user_stop.is_set():
        return False
    if resources.static_error is not None:
        return True
    if resources.static_done.is_set():
        return True
    process = resources.api_process
    return process is not None and process.poll() is not None


def _wait_for_readiness(resources: _Resources, hud_url: str, api_url: str) -> bool:
    deadline = time.monotonic() + READINESS_TIMEOUT
    ready = {hud_url: False, api_url: False}
    urls = (hud_url, api_url)
    while time.monotonic() < deadline:
        if resources.user_stop.is_set():
            return False
        if _failure(resources):
            return False
        remaining = max(0.0, deadline - time.monotonic())
        for url in urls:
            if ready[url]:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if _probe_url(url, min(0.25, remaining)):
                ready[url] = True
            if resources.user_stop.is_set() or _failure(resources):
                return False
        if all(ready.values()):
            return True
        resources.wakeup.wait(min(0.05, max(0.0, deadline - time.monotonic())))
        resources.wakeup.clear()
    return False


def _serve(resources: _Resources) -> None:
    assert resources.server is not None
    try:
        resources.server.serve_forever()
    except BaseException as error:
        resources.record_static_failure(error)
    finally:
        resources.static_done.set()
        resources.wakeup.set()


def _cleanup(resources: _Resources) -> None:
    """Release only this attempt's resources, in the prescribed order."""

    with resources._cleanup_lock:
        if resources._cleaned:
            return
        resources._cleaned = True

    server = resources.server
    if server is not None:
        if resources.server_thread_started:
            try:
                server.shutdown()
            except BaseException:
                pass
        try:
            server.server_close()
        except BaseException:
            pass
    thread = resources.server_thread
    if thread is not None:
        try:
            thread.join(JOIN_TIMEOUT)
        except BaseException:
            pass

    _terminate_child(resources.voice_process)
    _terminate_child(resources.api_process)


def _terminate_child(process: subprocess.Popen[object] | None) -> None:
    """Stop one owned child with bounded retries: terminate, then kill on timeout."""

    if process is None:
        return
    try:
        running = process.poll() is None
    except BaseException:
        running = True
    if running:
        try:
            process.terminate()
        except BaseException:
            pass
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass
    except BaseException:
        return
    try:
        process.kill()
    except BaseException:
        pass
    try:
        process.wait()
    except BaseException:
        pass


def _run(args: argparse.Namespace, model: Path, root: Path, resources: _Resources) -> int:
    hud_url = f"http://127.0.0.1:{args.hud_port}"
    page_url = f"{hud_url}/index.html"
    mode = _resolve_voice_input_mode(args)
    inherited = os.environ.copy()
    environ = inherited.copy()
    environ.update(
        {
            "JARVIS_API_PORT": str(API_PORT),
            "HUD_ORIGIN": hud_url,
            "JARVIS_VOICE_MODEL": str(model),
            "JARVIS_VOICE_INPUT_MODE": mode,
        }
    )
    handler = _make_handler(root / "hud")
    try:
        resources.server = _HudHTTPServer(("127.0.0.1", args.hud_port), handler)
    except Exception:
        print("jarvis-hud: could not bind HUD server", file=sys.stderr)
        return 1
    resources.server_thread = threading.Thread(
        target=functools.partial(_serve, resources),
        name="jarvis-hud-static",
        daemon=True,
    )
    try:
        resources.server_thread.start()
    except Exception:
        print("jarvis-hud: could not start HUD server", file=sys.stderr)
        return 1
    resources.server_thread_started = True
    resources.wakeup.wait(0)
    if _failure(resources) or resources.user_stop.is_set():
        return 0 if resources.user_stop.is_set() else 1

    terminal_mode = mode == "terminal"
    try:
        api_kwargs: dict[str, object] = {}
        if terminal_mode:
            api_kwargs["stdin"] = subprocess.DEVNULL
        resources.api_process = subprocess.Popen(
            [sys.executable, "-m", "jarvis.api.launcher"],
            cwd=str(root),
            env=environ,
            shell=False,
            **api_kwargs,
        )
    except Exception:
        print("jarvis-hud: could not start API", file=sys.stderr)
        return 1
    if not _wait_for_readiness(resources, page_url, f"http://127.0.0.1:{API_PORT}/vitals"):
        if resources.user_stop.is_set():
            return 0
        if resources.static_error is not None:
            print("jarvis-hud: static server failed before readiness", file=sys.stderr)
        elif resources.static_done.is_set():
            print("jarvis-hud: static server stopped before readiness", file=sys.stderr)
        else:
            process = resources.api_process
            status: object | None = None
            if process is not None:
                try:
                    status = process.poll()
                except BaseException:
                    status = None
            if status is not None:
                print(f"jarvis-hud: API exited before readiness (status {status})", file=sys.stderr)
            else:
                print("jarvis-hud: startup/readiness timed out", file=sys.stderr)
        return 1
    if _failure(resources):
        print("jarvis-hud: startup/readiness failed", file=sys.stderr)
        return 1

    print(page_url, flush=True)
    if not args.no_browser:
        try:
            opened = webbrowser.open_new_tab(page_url)
        except Exception:
            opened = False
        if not opened:
            print(f"jarvis-hud: warning: could not open {page_url}", file=sys.stderr)

    if terminal_mode:
        try:
            resources.voice_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "jarvis.orchestrator.voice_loop",
                    "--voice-model",
                    str(model),
                ],
                cwd=str(root),
                env=inherited,
                shell=False,
            )
        except Exception:
            print("jarvis-hud: could not start voice loop", file=sys.stderr)
            return 1

    while not resources.user_stop.is_set():
        voice_status = _voice_exit_status(resources)
        if voice_status is not None:
            print(f"jarvis-hud: voice loop exited (status {voice_status})", file=sys.stderr)
            return 1
        if _failure(resources):
            print("jarvis-hud: owned server stopped unexpectedly", file=sys.stderr)
            return 1
        resources.wakeup.wait(0.1)
        resources.wakeup.clear()
    return 0


def _voice_exit_status(resources: _Resources) -> object | None:
    """Return the voice child's exit status unless shutdown was requested."""

    if resources.user_stop.is_set():
        return None
    process = resources.voice_process
    if process is None:
        return None
    try:
        return process.poll()
    except BaseException:
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args is None:
        return 2
    root = _repo_root()
    try:
        model = _validate_preflight(args, os.environ, root)
        if model is None:
            return 2
        resources = _Resources()
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(signum: int, frame: object) -> None:
            del signum, frame
            resources.user_stop.set()
            resources.wakeup.set()

        signal.signal(signal.SIGTERM, handle_sigterm)
        try:
            return _run(args, model, root, resources)
        except KeyboardInterrupt:
            resources.user_stop.set()
            resources.wakeup.set()
            return 0
        except Exception:
            print("jarvis-hud: startup failed", file=sys.stderr)
            return 1
        finally:
            _cleanup(resources)
            signal.signal(signal.SIGTERM, previous_sigterm)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
