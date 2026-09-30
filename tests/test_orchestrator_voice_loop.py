import asyncio

import pytest

from jarvis.orchestrator import voice_loop


class FakeService:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.calls = 0
        self.close_calls = 0

    async def arun_once(self):
        self.calls += 1
        turn = next(self.turns)
        if isinstance(turn, BaseException):
            raise turn
        return turn

    async def aclose(self):
        self.close_calls += 1


def run_loop(service, output=None):
    if output is None:
        output = []
    asyncio.run(voice_loop.arun_loop(service, out=output.append))
    return output


def test_arun_loop_reuses_one_service_for_repeated_successful_turns():
    service = FakeService(["first", "second", EOFError()])

    assert run_loop(service) == ["first", "second"]
    assert service.calls == 3
    assert service.close_calls == 1


def test_arun_loop_reports_recoverable_exception_and_continues():
    boundaries = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    detail = "bad" + boundaries + "turn" + boundaries + "details"
    service = FakeService([ValueError(detail), "recovered", EOFError()])
    output = run_loop(service)

    assert output == [
        "voice loop error: ValueError: bad turn details",
        "recovered",
    ]
    assert all(not item.splitlines()[1:] for item in output)
    assert all(not any(char in item for char in boundaries) for item in output)
    assert service.calls == 3
    assert service.close_calls == 1


def test_arun_loop_closes_once_when_eof_is_the_first_turn():
    service = FakeService([EOFError()])

    assert run_loop(service) == []
    assert service.calls == 1
    assert service.close_calls == 1


@pytest.mark.parametrize("stop", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_arun_loop_stops_without_reporting_interrupts(stop):
    service = FakeService([stop])
    output = []

    with pytest.raises(type(stop)):
        asyncio.run(voice_loop.arun_loop(service, out=output.append))

    assert output == []
    assert service.calls == 1
    assert service.close_calls == 1


def test_arun_loop_closes_once_for_a_propagating_output_failure():
    service = FakeService(["response"])

    def failing_output(_response):
        raise RuntimeError("output failed")

    with pytest.raises(RuntimeError, match="output failed"):
        asyncio.run(voice_loop.arun_loop(service, out=failing_output))

    assert service.close_calls == 1


def test_main_constructs_one_cli_service_and_restores_sigterm(monkeypatch):
    constructed = []
    signal_calls = []
    previous = object()

    class MainService:
        def __init__(self, *, voice_model_path, runtime_source):
            constructed.append((voice_model_path, runtime_source))

    def fake_getsignal(signum):
        assert signum == voice_loop.signal.SIGTERM
        return previous

    def fake_signal(signum, handler):
        assert signum == voice_loop.signal.SIGTERM
        signal_calls.append(handler)

    async def fake_loop(_service):
        return None

    def fake_run(coro):
        coro.close()
        return None

    monkeypatch.setattr(voice_loop, "JarvisService", MainService)
    monkeypatch.setattr(voice_loop, "arun_loop", fake_loop)
    monkeypatch.setattr(voice_loop.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(voice_loop.signal, "signal", fake_signal)
    monkeypatch.setattr(voice_loop.asyncio, "run", fake_run)

    assert voice_loop.main(["--voice-model", "voice.onnx"]) == 0
    assert constructed == [("voice.onnx", "cli")]
    assert len(signal_calls) == 2
    assert signal_calls[0] is not previous
    assert signal_calls[1] is previous


def test_main_sigterm_handler_maps_to_clean_keyboard_interrupt(monkeypatch):
    signal_calls = []
    previous = object()

    class MainService:
        def __init__(self, **_kwargs):
            pass

        async def arun_once(self):
            signal_calls[0](voice_loop.signal.SIGTERM, None)

        async def aclose(self):
            self.closed = True

    def fake_signal(signum, handler):
        assert signum == voice_loop.signal.SIGTERM
        signal_calls.append(handler)

    monkeypatch.setattr(voice_loop, "JarvisService", MainService)
    monkeypatch.setattr(voice_loop.signal, "getsignal", lambda _signum: previous)
    monkeypatch.setattr(voice_loop.signal, "signal", fake_signal)

    def fake_run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(voice_loop.asyncio, "run", fake_run)

    assert voice_loop.main(["--voice-model", "voice.onnx"]) == 0
    assert len(signal_calls) == 2
    assert signal_calls[1] is previous


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt(), asyncio.CancelledError()])
def test_main_treats_top_level_stops_as_clean(monkeypatch, failure):
    calls = []
    previous = object()

    class MainService:
        def __init__(self, **_kwargs):
            calls.append("constructed")

    def fake_run(coro):
        coro.close()
        raise failure

    signal_calls = []
    monkeypatch.setattr(voice_loop, "JarvisService", MainService)
    monkeypatch.setattr(voice_loop.signal, "getsignal", lambda _signum: previous)
    monkeypatch.setattr(
        voice_loop.signal,
        "signal",
        lambda signum, handler: signal_calls.append((signum, handler)),
    )
    monkeypatch.setattr(voice_loop.asyncio, "run", fake_run)

    assert voice_loop.main(["--voice-model", "voice.onnx"]) == 0
    assert calls == ["constructed"]
    assert signal_calls[1][1] is previous


def test_main_returns_nonzero_for_unhandled_failure(monkeypatch):
    previous = object()
    signal_calls = []

    class MainService:
        def __init__(self, **_kwargs):
            pass

    def fake_run(coro):
        coro.close()
        raise RuntimeError("startup failed")

    monkeypatch.setattr(voice_loop, "JarvisService", MainService)
    monkeypatch.setattr(voice_loop.signal, "getsignal", lambda _signum: previous)
    monkeypatch.setattr(
        voice_loop.signal,
        "signal",
        lambda signum, handler: signal_calls.append((signum, handler)),
    )
    monkeypatch.setattr(voice_loop.asyncio, "run", fake_run)

    assert voice_loop.main(["--voice-model", "voice.onnx"]) == 1
    assert signal_calls[1][1] is previous
