# This file has been edited with the assistance of an AI tool.
"""
CLI-layer tests for agent_wrap.cli.run — argument parsing and calling protocol.

The ``run`` CLI is a thin wrapper: parse ``--base``, then delegate to
``services.launch_service.launch(use_base, claude_args)``.

``services.launch_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from __future__ import annotations

from agent_wrap.cli.run import run as agent_run
from agent_wrap.containers import services


def test_run_calls_launch_with_defaults() -> None:
    """Default call: use_base=False, no claude args."""
    services.launch_service.launch.return_value = 0
    rc = agent_run([])
    assert rc == 0
    services.launch_service.launch.assert_called_once_with(  # type: ignore[missing-attribute]
        use_base=False, claude_args=[]
    )


def test_run_passes_base_flag() -> None:
    """--base is parsed and forwarded to the service."""
    services.launch_service.launch.return_value = 0
    rc = agent_run(["--base"])
    assert rc == 0
    services.launch_service.launch.assert_called_once_with(  # type: ignore[missing-attribute]
        use_base=True, claude_args=[]
    )


def test_run_forwards_claude_args() -> None:
    """Remaining args after --base are forwarded as claude_args."""
    services.launch_service.launch.return_value = 0
    rc = agent_run(["--base", "-p", "hello", "--model", "sonnet"])
    assert rc == 0
    services.launch_service.launch.assert_called_once_with(  # type: ignore[missing-attribute]
        use_base=True, claude_args=["-p", "hello", "--model", "sonnet"]
    )


def test_run_forwards_exit_code() -> None:
    """The service's return code is propagated to the caller."""
    services.launch_service.launch.return_value = 42
    rc = agent_run(["--base"])
    assert rc == 42


def test_run_claude_args_only() -> None:
    """When --base is absent, all args are forwarded as claude_args."""
    services.launch_service.launch.return_value = 0
    rc = agent_run(["-p", "do a thing"])
    assert rc == 0
    services.launch_service.launch.assert_called_once_with(  # type: ignore[missing-attribute]
        use_base=False, claude_args=["-p", "do a thing"]
    )
