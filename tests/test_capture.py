from unittest.mock import patch

import numpy as np

from jarvis.voice.capture import record_on_enter


class FakeInputStream:
    instances = []
    events = []

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.events.append("open")
        self.instances.append(self)

    def start(self):
        self.events.append("start")
        self.callback(np.array([[0.1], [0.2]], dtype=np.float32), 2, None, None)

    def stop(self):
        self.events.append("stop")

    def close(self):
        self.events.append("close")


class StartFailureInputStream(FakeInputStream):
    def start(self):
        self.events.append("start")
        raise RuntimeError("start failed")


def setup_function():
    FakeInputStream.instances.clear()
    FakeInputStream.events.clear()


def test_stream_opens_after_first_input_and_closes_after_second():
    inputs = iter(("", ""))

    def press_enter():
        value = next(inputs)
        FakeInputStream.events.append(
            "input-start" if len(FakeInputStream.events) == 0 else "input-stop"
        )
        return value

    with patch("jarvis.voice.capture.sd.InputStream", FakeInputStream), patch(
        "builtins.input", side_effect=press_enter
    ):
        audio = record_on_enter()

    assert FakeInputStream.events == [
        "input-start",
        "open",
        "start",
        "input-stop",
        "stop",
        "close",
    ]
    assert audio.shape == (2,)
    assert audio.dtype == np.float32
    np.testing.assert_array_equal(audio, np.array([0.1, 0.2], dtype=np.float32))


def test_on_started_runs_after_start_before_stop_prompt():
    inputs = iter(("", ""))
    events = FakeInputStream.events

    def press_enter():
        value = next(inputs)
        events.append("input-start" if len(events) == 0 else "input-stop")
        return value

    def on_started():
        events.append("on-started")

    with patch("jarvis.voice.capture.sd.InputStream", FakeInputStream), patch(
        "builtins.input", side_effect=press_enter
    ):
        record_on_enter(on_started=on_started)

    assert events == [
        "input-start",
        "open",
        "start",
        "on-started",
        "input-stop",
        "stop",
        "close",
    ]


def test_default_does_not_write_a_file(tmp_path):
    with patch("jarvis.voice.capture.sd.InputStream", FakeInputStream), patch(
        "builtins.input", side_effect=("", "")
    ), patch("jarvis.voice.capture.wave.open") as wave_open:
        audio = record_on_enter(save_path=None)

    assert audio.dtype == np.float32
    assert list(tmp_path.iterdir()) == []
    wave_open.assert_not_called()


def test_save_path_writes_wav(tmp_path):
    output = tmp_path / "out.wav"
    with patch("jarvis.voice.capture.sd.InputStream", FakeInputStream), patch(
        "builtins.input", side_effect=("", "")
    ):
        record_on_enter(save_path=output)

    assert output.exists()


def test_start_failure_closes_without_stopping():
    with patch("jarvis.voice.capture.sd.InputStream", StartFailureInputStream), patch(
        "builtins.input", side_effect=("",)
    ):
        try:
            record_on_enter()
        except RuntimeError as error:
            assert str(error) == "start failed"
        else:
            raise AssertionError("record_on_enter() did not propagate start failure")

    assert FakeInputStream.events == ["open", "start", "close"]


def test_on_started_is_not_called_when_start_fails():
    called = []
    with patch("jarvis.voice.capture.sd.InputStream", StartFailureInputStream), patch(
        "builtins.input", side_effect=("",)
    ):
        try:
            record_on_enter(on_started=lambda: called.append(True))
        except RuntimeError:
            pass
        else:
            raise AssertionError("record_on_enter() did not propagate start failure")

    assert called == []
    assert FakeInputStream.events == ["open", "start", "close"]


def test_on_started_failure_does_not_change_capture_or_cleanup(capsys):
    with patch("jarvis.voice.capture.sd.InputStream", FakeInputStream), patch(
        "builtins.input", side_effect=("", "")
    ):
        audio = record_on_enter(on_started=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert FakeInputStream.events == [
        "open",
        "start",
        "stop",
        "close",
    ]
    np.testing.assert_array_equal(audio, np.array([0.1, 0.2], dtype=np.float32))
    assert "capture-start callback failed: boom" in capsys.readouterr().err
