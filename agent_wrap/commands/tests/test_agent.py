# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.commands."""

from __future__ import annotations

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


def test_empty_is_false():
    assert not _is_truthy("")


def test_zero_is_false():
    assert not _is_truthy("0")


def test_false_is_false():
    assert not _is_truthy("false")
    assert not _is_truthy("FALSE")


def test_no_is_false():
    assert not _is_truthy("no")
    assert not _is_truthy("NO")


def test_one_is_true():
    assert _is_truthy("1")


def test_yes_is_true():
    assert _is_truthy("yes")


def test_any_string_is_true():
    assert _is_truthy("hello")
