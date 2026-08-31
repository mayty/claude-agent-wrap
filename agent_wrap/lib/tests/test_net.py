# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/net.py."""

import socket
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import PortUnavailableError
from agent_wrap.lib.net import find_free_port

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def bound_port() -> Iterator[socket.socket]:
    """Hold a real bound listener, yielding the socket so the port stays occupied."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("", 0))
        sock.listen(1)
        yield sock


def test_returns_the_base_port_when_free() -> None:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("", 0))
        free = probe.getsockname()[1]
    # The port is released before the scan, so the base is the first hit.
    assert find_free_port(free, 50) == free


def test_skips_an_occupied_base_port(bound_port: socket.socket) -> None:
    taken = bound_port.getsockname()[1]
    assert find_free_port(taken, 50) > taken


def test_raises_when_the_whole_range_is_taken(bound_port: socket.socket) -> None:
    taken = bound_port.getsockname()[1]
    # A window of exactly one port, and that port is held.
    with pytest.raises(PortUnavailableError, match=f"no free TCP port in range .{taken}"):
        find_free_port(taken, 1)
