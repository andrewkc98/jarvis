"""Tests for jarvis.tools.vitals."""

from unittest.mock import MagicMock, patch

from jarvis.tools.vitals import get_vitals


def test_get_vitals_returns_expected_shape_and_values():
    fake_vm = MagicMock()
    fake_vm.percent = 42.0
    fake_disk = MagicMock()
    fake_disk.percent = 77.5

    with (
        patch("jarvis.tools.vitals.psutil.cpu_percent", return_value=12.5) as mock_cpu,
        patch("jarvis.tools.vitals.psutil.virtual_memory", return_value=fake_vm) as mock_vm,
        patch("jarvis.tools.vitals.psutil.disk_usage", return_value=fake_disk) as mock_disk,
    ):
        result = get_vitals()

    assert set(result) == {"cpu_percent", "ram_percent", "disk_percent", "gpu"}
    assert result["cpu_percent"] == 12.5
    assert result["ram_percent"] == 42.0
    assert result["disk_percent"] == 77.5
    assert result["gpu"] == "unavailable"

    mock_cpu.assert_called_once_with(interval=None)
    mock_vm.assert_called_once_with()
    mock_disk.assert_called_once_with("/")


def test_get_vitals_gpu_is_always_unavailable_string():
    with (
        patch("jarvis.tools.vitals.psutil.cpu_percent", return_value=0.0),
        patch("jarvis.tools.vitals.psutil.virtual_memory") as mock_vm,
        patch("jarvis.tools.vitals.psutil.disk_usage") as mock_disk,
    ):
        mock_vm.return_value.percent = 0.0
        mock_disk.return_value.percent = 0.0
        result = get_vitals()

    assert result["gpu"] is "unavailable"
