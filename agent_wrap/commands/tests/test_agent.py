# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.commands."""

from __future__ import annotations

import pytest

from agent_wrap.commands.agent import _extract_network, _is_truthy


def test_no_network():
    assert _extract_network([]) is None
    assert _extract_network(["--device", "/dev/fuse"]) is None


def test_separate_flag():
    assert _extract_network(["--network", "mynet"]) == "mynet"


def test_equals_syntax():
    assert _extract_network(["--network=mynet"]) == "mynet"


def test_net_alias():
    assert _extract_network(["--net", "mynet"]) == "mynet"
    assert _extract_network(["--net=mynet"]) == "mynet"


def test_first_occurrence_wins():
    assert _extract_network(["--network", "first", "--network", "second"]) == "first"


def test_missing_value():
    assert _extract_network(["--network"]) is None


def test_among_other_flags():
    args = ["--device", "/dev/fuse", "--network", "mynet", "--cap-add", "SYS_ADMIN"]
    assert _extract_network(args) == "mynet"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "FALSE", "NO"])
def test_truthy_false(value: str) -> None:
    assert not _is_truthy(value)


@pytest.mark.parametrize("value", ["1", "yes", "YES", "hello", "true"])
def test_truthy_true(value: str) -> None:
    assert _is_truthy(value)
