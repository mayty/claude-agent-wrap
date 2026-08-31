# This file has been created with the assistance of an AI tool.
"""Tests for lib/process_utils."""

from typing import TYPE_CHECKING

from agent_wrap.lib.process_utils import pid_alive

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_pid_alive_true_for_running(mocker: MockerFixture):
    mocker.patch("os.kill", return_value=None)
    assert pid_alive(123) is True


def test_pid_alive_false_for_dead(mocker: MockerFixture):
    def _kill(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    mocker.patch("os.kill", _kill)
    assert pid_alive(123) is False


def test_pid_alive_true_for_permission_error(mocker: MockerFixture):
    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError

    mocker.patch("os.kill", _kill)
    assert pid_alive(123) is True


def test_pid_alive_false_for_zombie(mocker: MockerFixture):
    """Zombie processes (state 'Z' in /proc/<pid>/stat) should be reported as dead."""
    mocker.patch("os.kill", return_value=None)
    mock_path = mocker.MagicMock()
    mock_path.read_text.return_value = "123 (python3) Z 100 100 0 0 -1 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    mocker.patch("agent_wrap.lib.process_utils.Path", return_value=mock_path)
    assert pid_alive(123) is False


def test_pid_alive_true_for_running_non_zombie(mocker: MockerFixture):
    """Processes in state 'S' (sleeping) should be reported as alive."""
    mocker.patch("os.kill", return_value=None)
    mock_path = mocker.MagicMock()
    mock_path.read_text.return_value = "123 (python3) S 100 100 0 0 -1 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    mocker.patch("agent_wrap.lib.process_utils.Path", return_value=mock_path)
    assert pid_alive(123) is True


def test_pid_alive_falls_back_when_proc_missing(mocker: MockerFixture):
    """When /proc/<pid>/stat is unreadable, fall back to the os.kill(0) result."""
    mocker.patch("os.kill", return_value=None)
    mock_path = mocker.MagicMock()
    mock_path.read_text.side_effect = FileNotFoundError
    mocker.patch("agent_wrap.lib.process_utils.Path", return_value=mock_path)
    assert pid_alive(123) is True
