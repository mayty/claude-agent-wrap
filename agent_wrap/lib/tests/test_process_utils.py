# This file has been created with the assistance of an AI tool.
"""Tests for lib/process_utils."""

from __future__ import annotations

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
