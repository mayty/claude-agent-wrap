# This file has been created with the assistance of an AI tool.
"""
TCP port helpers.

A sidecar that shares the host network namespace cannot use a fixed port: a second
provider's sidecar would collide with the first. :func:`find_free_port` resolves one at
cold start by probing upward from a preferred base.

The probe binds and immediately closes, so it proves only that the port was free at that
instant. Callers must serialize their own scans (agent-wrap does this under the shared
sidecar lock) to stop two of them choosing the same port; a *foreign* process taking the
port between the probe and the real bind stays possible, and shows up as the started
service failing its health check.
"""

import socket
from contextlib import closing

from agent_wrap.exceptions import PortUnavailableError


def find_free_port(start: int, limit: int) -> int:
    """
    Return the first bindable TCP port in ``[start, start + limit)``.

    Probes with an ``0.0.0.0`` bind, which conflicts with an existing ``127.0.0.1``
    binding and vice versa — the stricter test, and the right one for a server that
    will itself listen on all interfaces.

    Raises :class:`PortUnavailableError` when every port in the range is taken.
    """
    for port in range(start, start + limit):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            try:
                probe.bind(("", port))
            except OSError:
                continue
            return port
    msg = f"no free TCP port in range [{start}, {start + limit})"
    raise PortUnavailableError(msg)
