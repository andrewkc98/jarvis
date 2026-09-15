from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
from threading import Thread as RealThread

import pytest

from jarvis import hud_launcher as launcher


def _args(**kwargs: object) -> argparse.Namespace:
    values = {"voice_model": None, "hud_port": 4173, "no_browser": True}
    values.update(kwargs)
    return argparse.Namespace(**values)


def _fake_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_is_readable_regular_file", lambda path: True)


def test_model_precedence_and_normalization(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    env = {"JARVIS_VOICE_MODEL": "env.onnx"}
    assert launcher._resolve_voice_model("explicit.onnx", env, root) == Path.cwd() / "explicit.onnx"
    assert launcher._resolve_voice_model(None, env, root) == Path.cwd() / "env.onnx"
    assert launcher._resolve_voice_model(None, {}, root) == (root / "en_US-lessac-medium.onnx").resolve()


def test_empty_model_option_and_files_fail_without_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    args = _args(voice_model="")
    assert launcher._validate_preflight(args, {"JARVIS_VOICE_MODEL": "fallback.onnx"}, root) is None
    assert "invalid voice-model" in capsys.readouterr().err
    monkeypatch.setattr(launcher, "_is_readable_regular_file", lambda path: False)
    assert launcher._validate_preflight(_args(), {}, root) is None


@pytest.mark.parametrize("bad", ["", " ", "8764", "0", "65536", "abc", "8765 ", "+8765"])
def test_inherited_api_port_is_strict(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    _fake_files(monkeypatch)
    args = _args()
    assert launcher._validate_preflight(args, {"JARVIS_API_PORT": bad}, Path.cwd()) is None


@pytest.mark.parametrize("bad", ["", " ", "0", "8765", "65536", "nope", "4173 "])
def test_hud_port_validation(bad: str) -> None:
    assert launcher._parse_args(["--hud-port", bad]) is None


def test_preflight_companion_is_literal_suffix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    seen: list[Path] = []
    monkeypatch.setattr(launcher, "_is_readable_regular_file", lambda path: seen.append(path) or True)
    model = root / "voice.onnx"
    assert launcher._validate_preflight(_args(voice_model=str(model)), {}, root) == model.resolve()
    assert seen == [model.resolve(), Path(str(model.resolve()) + ".json")]


class FakeServer:
    instances: list["FakeServer"] = []

    def __init__(self, address: tuple[str, int], handler: object) -> None:
        self.address = address
        self.handler = handler
        self.calls: list[str] = []
        self.serve_error: BaseException | None = None
        type(self).instances.append(self)

    def serve_forever(self) -> None:
        if self.serve_error is not None:
            raise self.serve_error

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def server_close(self) -> None:
        self.calls.append("close")


class FakeThread:
    instances: list["FakeThread"] = []

    def __init__(
        self,
        *,
        target: object,
        name: str,
        daemon: bool,
        run_target: bool = False,
        start_error: BaseException | None = None,
    ) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.run_target = run_target
        self.start_error = start_error
        self.calls: list[str] = []
        self._alive = False
        type(self).instances.append(self)

    def start(self) -> None:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error
        self._alive = True
        if self.run_target:
            try:
                self.target()  # type: ignore[operator]
            finally:
                self._alive = False

    def join(self, timeout: float) -> None:
        self.calls.append(f"join:{timeout}")
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


def _fake_thread_factory(
    *, run_target: bool = False, start_error: BaseException | None = None
) -> object:
    def factory(**kwargs: object) -> FakeThread:
        return FakeThread(
            target=kwargs["target"],
            name=kwargs["name"],
            daemon=bool(kwargs["daemon"]),
            run_target=run_target,
            start_error=start_error,
        )

    return factory


class FakeProcess:
    instances: list["FakeProcess"] = []

    def __init__(self, exit_after: int | None = None) -> None:
        self.calls: list[str] = []
        self.running = True
        self.poll_count = 0
        self.exit_after = exit_after
        type(self).instances.append(self)

    def poll(self) -> int | None:
        self.poll_count += 1
        if self.exit_after is not None and self.poll_count >= self.exit_after:
            self.running = False
        return None if self.running else 0

    def terminate(self) -> None:
        self.calls.append("terminate")
        self.running = False

    def kill(self) -> None:
        self.calls.append("kill")
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait" if timeout is None else f"wait:{timeout}")
        self.running = False
        return 0


def _run_with_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, no_browser: bool = True) -> tuple[int, FakeServer, FakeProcess]:
    FakeServer.instances.clear()
    FakeProcess.instances.clear()
    FakeThread.instances.clear()
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: FakeProcess(exit_after=5))
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)
    monkeypatch.setattr(launcher, "webbrowser", type("Browser", (), {"open_new_tab": staticmethod(lambda url: True)}))
    resources = launcher._Resources()
    result = launcher._run(_args(no_browser=no_browser), tmp_path, tmp_path, resources)
    launcher._cleanup(resources)
    return result, FakeServer.instances[0], FakeProcess.instances[0]


def test_bind_precedes_spawn_and_child_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []

    class OrderedServer(FakeServer):
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            order.append("bind")
            super().__init__(address, handler)

    class OrderedProcess(FakeProcess):
        def __init__(self) -> None:
            order.append("spawn")
            super().__init__(exit_after=5)

    monkeypatch.setattr(launcher, "_HudHTTPServer", OrderedServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: (order.append("popen"), OrderedProcess())[1])
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)
    monkeypatch.setattr(launcher, "webbrowser", type("Browser", (), {"open_new_tab": staticmethod(lambda url: True)}))
    resources = launcher._Resources()
    result = launcher._run(_args(), tmp_path, tmp_path, resources)
    launcher._cleanup(resources)
    assert result == 1
    assert order[:2] == ["bind", "popen"]


def test_readiness_uses_only_two_urls_and_browser_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    urls: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: FakeProcess(exit_after=5))
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: urls.append(url) or True)
    monkeypatch.setattr(launcher.webbrowser, "open_new_tab", lambda url: opened.append(url) or True)
    resources = launcher._Resources()
    result = launcher._run(_args(no_browser=False), tmp_path, tmp_path, resources)
    launcher._cleanup(resources)
    assert result == 1
    assert set(urls) == {"http://127.0.0.1:4173/index.html", "http://127.0.0.1:8765/vitals"}
    assert opened == ["http://127.0.0.1:4173/index.html"]


def test_cleanup_order_and_timeout_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = launcher._Resources()
    server = FakeServer(("127.0.0.1", 4173), object())
    process = FakeProcess()
    resources.server = server
    resources.api_process = process

    class RecordingThread(FakeThread):
        def join(self, timeout: float) -> None:
            super().join(timeout)
            server.calls.append("join")

    resources.server_thread = RecordingThread(target=lambda: None, name="jarvis-hud-static", daemon=True)
    resources.server_thread_started = True
    launcher._cleanup(resources)
    launcher._cleanup(resources)
    assert server.calls == ["shutdown", "close", "join"]
    assert process.calls == ["terminate", "wait:5.0"]


def test_static_bind_failure_spawns_no_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "_HudHTTPServer", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    called = False
    def no_spawn(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("spawned")
    monkeypatch.setattr(launcher.subprocess, "Popen", no_spawn)
    resources = launcher._Resources()
    assert launcher._run(_args(), tmp_path, tmp_path, resources) == 1
    assert not called


def test_main_thread_start_failure_closes_bound_server_without_shutdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class BoundServer(FakeServer):
        pass

    FakeServer.instances.clear()
    spawned = False
    monkeypatch.setattr(launcher, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_validate_preflight", lambda args, environ, root: tmp_path / "voice.onnx")
    monkeypatch.setattr(launcher, "_HudHTTPServer", BoundServer)
    monkeypatch.setattr(
        launcher.threading,
        "Thread",
        _fake_thread_factory(start_error=RuntimeError("start failed after bind")),
    )
    monkeypatch.setattr(launcher.signal, "signal", lambda signum, handler: signal.SIG_DFL)

    def no_spawn(*args: object, **kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawned")

    monkeypatch.setattr(launcher.subprocess, "Popen", no_spawn)

    result: list[int] = []

    def invoke_main() -> None:
        result.append(launcher.main(["--no-browser"]))

    caller = RealThread(target=invoke_main, daemon=True)
    caller.start()
    caller.join(1.0)
    if caller.is_alive():
        pytest.fail("launcher.main() did not terminate within 1 second")
    assert result and result[0] != 0
    server = BoundServer.instances[-1]
    assert server.calls == ["close"]
    assert not spawned
    assert "could not start HUD server" in capsys.readouterr().err


def test_early_api_exit_reports_only_numeric_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class EarlyProcess(FakeProcess):
        def poll(self) -> int | None:
            self.running = False
            return 37

    monkeypatch.setattr(launcher, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_validate_preflight", lambda args, environ, root: tmp_path / "argv-secret.onnx")
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: EarlyProcess())
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)
    monkeypatch.setattr(
        launcher.os,
        "environ",
        {"JARVIS_VOICE_MODEL": "environment-secret", "API_SECRET": "environment-secret"},
    )

    assert launcher.main(["--voice-model", "argv-secret", "--no-browser"]) != 0
    error = capsys.readouterr().err
    assert "status 37" in error
    assert "environment-secret" not in error
    assert "argv-secret" not in error


def test_readiness_recomputes_deadline_before_each_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = launcher._Resources()
    now = [0.0]
    probes: list[tuple[str, float]] = []

    def monotonic() -> float:
        return now[0]

    def probe(url: str, timeout: float) -> bool:
        probes.append((url, timeout))
        now[0] = launcher.READINESS_TIMEOUT
        return False

    monkeypatch.setattr(launcher.time, "monotonic", monotonic)
    monkeypatch.setattr(launcher, "_probe_url", probe)
    assert not launcher._wait_for_readiness(resources, "hud", "api")
    assert probes == [("hud", 0.25)]


def test_module_entry_point_uses_system_exit() -> None:
    source = Path(launcher.__file__).read_text()
    assert "raise SystemExit(main())" in source


def _copy_wrapper_harness(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "copied-repo"
    wrapper = repo / "jarvis-hud"
    (repo / ".venv" / "bin").mkdir(parents=True)
    wrapper.write_text((Path(__file__).parents[1] / "jarvis-hud").read_text())
    wrapper.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    return wrapper, outside


def test_repository_wrapper_selects_relative_interpreter_forwards_args_and_execs(tmp_path: Path) -> None:
    wrapper, outside = _copy_wrapper_harness(tmp_path)
    fake_python = wrapper.parent / ".venv" / "bin" / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "printf 'pid=%s\\n' \"$$\"\n"
        "printf 'argc=%s\\n' \"$#\"\n"
        "for arg do printf 'arg=<%s>\\n' \"$arg\"; done\n"
    )
    fake_python.chmod(0o755)

    forwarded = ["--voice-model", "model with spaces.onnx", "", "--no-browser"]
    process = subprocess.Popen(
        [str(wrapper), *forwarded],
        cwd=outside,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()

    assert process.returncode == 0
    assert stderr == ""
    lines = stdout.splitlines()
    assert lines[0] == f"pid={process.pid}"
    assert lines[1] == "argc=6"
    assert lines[2:] == [
        "arg=<-m>",
        "arg=<jarvis.hud_launcher>",
        "arg=<--voice-model>",
        "arg=<model with spaces.onnx>",
        "arg=<>",
        "arg=<--no-browser>",
    ]


@pytest.mark.parametrize("make_interpreter", [False, True], ids=["missing", "non-executable"])
def test_repository_wrapper_reports_missing_or_non_executable_interpreter(
    tmp_path: Path, make_interpreter: bool
) -> None:
    wrapper, outside = _copy_wrapper_harness(tmp_path)
    if make_interpreter:
        fake_python = wrapper.parent / ".venv" / "bin" / "python"
        fake_python.write_text("#!/bin/sh\nexit 0\n")
        fake_python.chmod(0o644)

    result = subprocess.run([str(wrapper)], cwd=outside, capture_output=True, text=True)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "jarvis-hud: run 'python -m venv .venv' from the repository root first\n"


@pytest.mark.parametrize("target", ["model", "companion"])
@pytest.mark.parametrize("state", ["missing", "non-regular", "unreadable"])
def test_main_rejects_each_model_and_companion_file_state_before_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target: str,
    state: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    model = root / "voice.onnx"
    companion = Path(str(model) + ".json")
    checks: list[Path] = []
    file_states = {
        model.resolve(): state if target == "model" else "readable",
        companion.resolve(): state if target == "companion" else "readable",
    }
    server_constructed = False
    child_spawned = False

    def fake_readable(path: Path) -> bool:
        checks.append(path)
        status = file_states[path]
        if status == "missing":
            return False
        mode = launcher.stat.S_IFDIR if status == "non-regular" else launcher.stat.S_IFREG
        if not launcher.stat.S_ISREG(mode):
            return False
        return status != "unreadable"

    def no_server(*args: object, **kwargs: object) -> object:
        nonlocal server_constructed
        server_constructed = True
        raise AssertionError("server constructed before preflight rejection")

    def no_child(*args: object, **kwargs: object) -> object:
        nonlocal child_spawned
        child_spawned = True
        raise AssertionError("child spawned before preflight rejection")

    monkeypatch.setattr(launcher, "_repo_root", lambda: root)
    monkeypatch.setattr(launcher, "_is_readable_regular_file", fake_readable)
    monkeypatch.setattr(launcher, "_HudHTTPServer", no_server)
    monkeypatch.setattr(launcher.subprocess, "Popen", no_child)

    assert launcher.main(["--voice-model", str(model), "--no-browser"]) == 2
    assert not server_constructed
    assert not child_spawned
    assert checks == ([model.resolve()] if target == "model" else [model.resolve(), companion.resolve()])
    assert "voice model" in capsys.readouterr().err


def test_static_handler_server_and_thread_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hud_root = root / "hud"
    hud_root.mkdir(parents=True)
    captured: dict[str, object] = {}
    expected_daemon_threads = launcher._HudHTTPServer.daemon_threads

    class CapturingServer(FakeServer):
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            captured["address"] = address
            captured["handler"] = handler
            super().__init__(address, handler)

    process = FakeProcess()
    FakeThread.instances.clear()
    monkeypatch.setattr(launcher, "_HudHTTPServer", CapturingServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)
    resources = launcher._Resources()
    resources.user_stop.set()
    result = launcher._run(_args(), root / "voice.onnx", root, resources)
    launcher._cleanup(resources)

    assert result == 0
    assert captured["address"] == ("127.0.0.1", 4173)
    thread = FakeThread.instances[-1]
    assert thread.daemon is True
    assert thread.name == "jarvis-hud-static"
    assert callable(thread.target)
    assert expected_daemon_threads is True

    seen_directory: list[str] = []
    def capture_handler_init(self: object, *args: object, **kwargs: object) -> None:
        seen_directory.append(str(kwargs.get("directory")))

    monkeypatch.setattr(launcher.http.server.SimpleHTTPRequestHandler, "__init__", capture_handler_init)
    handler = captured["handler"]
    handler(None, None, None)  # type: ignore[operator]
    assert seen_directory == [str(hud_root)]


def test_child_spawn_contract_and_environment_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    process = FakeProcess()
    captured: dict[str, object] = {}

    def capture_popen(*args: object, **kwargs: object) -> FakeProcess:
        captured["argv"] = args[0]
        captured.update(kwargs)
        return process

    sentinel = "inherited-environment-sentinel"
    monkeypatch.setattr(launcher.os, "environ", {"UNRELATED_SENTINEL": sentinel})
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: resources.user_stop.set() or True)
    resources = launcher._Resources()
    result = launcher._run(_args(), root / "voice.onnx", root, resources)
    launcher._cleanup(resources)

    assert result == 0
    assert captured["argv"] == [os.sys.executable, "-m", "jarvis.api.launcher"]
    assert captured["shell"] is False
    assert captured["cwd"] == str(root)
    child_env = captured["env"]
    assert child_env["JARVIS_API_PORT"] == "8765"  # type: ignore[index]
    assert child_env["HUD_ORIGIN"] == "http://127.0.0.1:4173"  # type: ignore[index]
    assert child_env["JARVIS_VOICE_MODEL"] == str((root / "voice.onnx"))  # type: ignore[index]
    assert child_env["UNRELATED_SENTINEL"] == sentinel  # type: ignore[index]
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err


@pytest.mark.parametrize("browser_result", [False, "raise"], ids=["false", "exception"])
def test_browser_failure_is_nonfatal_until_fake_user_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    browser_result: bool | str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    process = FakeProcess()
    resources = launcher._Resources()

    class StopAfterWait:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, timeout: float) -> bool:
            del timeout
            self.calls += 1
            if self.calls >= 2:
                resources.user_stop.set()
            return False

        def clear(self) -> None:
            return None

    def open_browser(url: str) -> bool:
        if browser_result == "raise":
            raise RuntimeError("browser unavailable")
        return browser_result

    resources.wakeup = StopAfterWait()  # type: ignore[assignment]
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)
    monkeypatch.setattr(launcher.webbrowser, "open_new_tab", open_browser)

    result = launcher._run(_args(no_browser=False), root / "voice.onnx", root, resources)
    launcher._cleanup(resources)

    assert result == 0
    assert "warning: could not open http://127.0.0.1:4173/index.html" in capsys.readouterr().err


def test_no_browser_suppresses_browser_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resources = launcher._Resources()
    process = FakeProcess()
    opened = False

    class StopAfterWait:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, timeout: float) -> bool:
            del timeout
            self.calls += 1
            if self.calls >= 2:
                resources.user_stop.set()
            return False

        def clear(self) -> None:
            return None

    def unexpected_open(url: str) -> bool:
        nonlocal opened
        opened = True
        raise AssertionError(url)

    resources.wakeup = StopAfterWait()  # type: ignore[assignment]
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)
    monkeypatch.setattr(launcher.webbrowser, "open_new_tab", unexpected_open)

    assert launcher._run(_args(no_browser=True), tmp_path / "voice.onnx", tmp_path, resources) == 0
    launcher._cleanup(resources)
    assert not opened


@pytest.mark.parametrize("failure", ["timeout", "static", "child"])
def test_startup_failures_return_nonzero_and_cleanup_owned_resources_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    FakeServer.instances.clear()
    process = FakeProcess()
    resources = launcher._Resources()

    class StaticFailureServer(FakeServer):
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            super().__init__(address, handler)
            self.serve_error = RuntimeError("static failed")

    class RecordingThread(FakeThread):
        def join(self, timeout: float) -> None:
            super().join(timeout)
            if FakeServer.instances:
                FakeServer.instances[-1].calls.append("join")

    def make_thread(**kwargs: object) -> RecordingThread:
        return RecordingThread(
            target=kwargs["target"],
            name=kwargs["name"],
            daemon=bool(kwargs["daemon"]),
            run_target=failure == "static",
        )

    monkeypatch.setattr(launcher, "_HudHTTPServer", StaticFailureServer if failure == "static" else FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", make_thread)
    if failure == "child":
        class EarlyProcess(FakeProcess):
            def poll(self) -> int | None:
                self.running = False
                return 19
        process = EarlyProcess()
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    if failure == "timeout":
        ticks = iter([0.0, 0.0, 10.0, 10.0])
        monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks, 10.0))
        monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: False)
    else:
        monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: True)

    result = launcher._run(_args(), tmp_path, tmp_path, resources)
    launcher._cleanup(resources)
    launcher._cleanup(resources)

    assert result == 1
    server = FakeServer.instances[-1]
    assert server.calls == ["shutdown", "close", "join"]
    if failure == "static":
        assert process.calls == []
    elif failure == "timeout":
        assert process.calls == ["terminate", "wait:5.0"]
    else:
        assert process.calls == ["wait:5.0"]


def test_fake_user_stop_returns_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resources = launcher._Resources()
    process = FakeProcess()

    def ready_and_stop(current: launcher._Resources, *urls: str) -> bool:
        current.user_stop.set()
        return True

    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(launcher, "_wait_for_readiness", ready_and_stop)
    assert launcher._run(_args(), tmp_path, tmp_path, resources) == 0
    launcher._cleanup(resources)


def test_keyboard_interrupt_returns_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_validate_preflight", lambda args, environ, root: tmp_path / "voice.onnx")
    monkeypatch.setattr(launcher, "_run", lambda args, model, root, resources: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert launcher.main(["--no-browser"]) == 0


def test_sigterm_returns_zero_and_restores_previous_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(launcher, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_validate_preflight", lambda args, environ, root: tmp_path / "voice.onnx")

    def run_and_handle_sigterm(args: argparse.Namespace, model: Path, root: Path, resources: launcher._Resources) -> int:
        del args, model, root
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)  # type: ignore[misc]
        assert resources.user_stop.is_set()
        return 0

    monkeypatch.setattr(launcher, "_run", run_and_handle_sigterm)
    assert launcher.main(["--no-browser"]) == 0
    assert signal.getsignal(signal.SIGTERM) is previous


def test_cleanup_timeout_expired_terminates_then_kills_only_owned_child() -> None:
    class TimeoutProcess:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.calls.append("terminate")

        def kill(self) -> None:
            self.calls.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            self.calls.append("wait" if timeout is None else f"wait:{timeout}")
            if timeout is not None:
                raise subprocess.TimeoutExpired("owned-api", timeout)
            return 0

    owned = TimeoutProcess()
    unrelated = TimeoutProcess()
    resources = launcher._Resources()
    resources.api_process = owned  # type: ignore[assignment]
    launcher._cleanup(resources)
    assert owned.calls == ["terminate", "wait:5.0", "kill", "wait"]
    assert unrelated.calls == []


def test_unrelated_api_listener_is_never_operated_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class UnrelatedListener:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def poll(self) -> None:
            self.calls.append("poll")
            raise AssertionError("unrelated listener discovered")

        def terminate(self) -> None:
            self.calls.append("terminate")
            raise AssertionError("unrelated listener terminated")

        def kill(self) -> None:
            self.calls.append("kill")
            raise AssertionError("unrelated listener killed")

    unrelated = UnrelatedListener()
    owned = FakeProcess()
    resources = launcher._Resources()
    resources.server = FakeServer(("127.0.0.1", 4173), object())
    resources.server_thread = FakeThread(target=lambda: None, name="jarvis-hud-static", daemon=True)
    resources.server_thread_started = True
    monkeypatch.setattr(launcher, "_HudHTTPServer", FakeServer)
    monkeypatch.setattr(launcher.threading, "Thread", _fake_thread_factory())
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: owned)
    monkeypatch.setattr(launcher, "_probe_url", lambda url, timeout: resources.user_stop.set() or True)

    assert launcher._run(_args(), tmp_path, tmp_path, resources) == 0
    launcher._cleanup(resources)
    assert unrelated.calls == []
    assert owned.calls == ["terminate", "wait:5.0"]
