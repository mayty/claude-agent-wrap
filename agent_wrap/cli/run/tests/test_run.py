# This file has been edited with the assistance of an AI tool.
"""
CLI-layer tests for agent_wrap.cli.run — argument parsing and calling protocol.

The ``run`` CLI is a thin wrapper: parse ``--base``, then delegate to
``services.launch_service.launch(use_base, claude_args)``. Everything it does not
recognise is forwarded verbatim, which is what the pass-through tests below pin.

``services.launch_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.containers import services

if TYPE_CHECKING:
    from click.testing import CliRunner


def test_run_calls_launch_with_defaults(runner: CliRunner) -> None:
    """Default call: use_base=False, no claude args."""
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run"])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(use_base=False, claude_args=[])  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("flag", ["--base", "-b"])
def test_run_passes_base_flag(runner: CliRunner, flag: str) -> None:
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", flag])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(use_base=True, claude_args=[])  # pyrefly: ignore [missing-attribute]


def test_run_forwards_claude_args(runner: CliRunner) -> None:
    """Remaining args after --base are forwarded as claude_args."""
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", "--base", "-p", "hello", "--model", "sonnet"])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        use_base=True, claude_args=["-p", "hello", "--model", "sonnet"]
    )


def test_run_forwards_exit_code(runner: CliRunner) -> None:
    """The service's return code is propagated to the caller."""
    services.launch_service.launch.return_value = 42  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", "--base"])
    assert result.exit_code == 42


def test_run_claude_args_only(runner: CliRunner) -> None:
    """When --base is absent, all args are forwarded as claude_args."""
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", "-p", "do a thing"])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        use_base=False, claude_args=["-p", "do a thing"]
    )


def test_run_forwards_help_to_claude_code(runner: CliRunner) -> None:
    """``add_help_option=False``: --help is Claude Code's flag here, not the wrapper's."""
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", "--help"])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        use_base=False, claude_args=["--help"]
    )


def test_run_consumes_base_after_an_unknown_flag(runner: CliRunner) -> None:
    """Matches argparse's parse_known_args: -b is the wrapper's wherever it appears."""
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", "-p", "hi", "-b"])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        use_base=True, claude_args=["-p", "hi"]
    )


def test_run_forwards_unknown_long_flags_verbatim(runner: CliRunner) -> None:
    services.launch_service.launch.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["run", "--model", "foo", "-b", "bar"])
    assert result.exit_code == 0
    services.launch_service.launch.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        use_base=True, claude_args=["--model", "foo", "bar"]
    )


def test_run_takes_a_registry_write_grant(runner: CliRunner, write_grants: list[str]) -> None:
    """A launch registers its project directory, so it is one of the two verbs that may."""
    runner.invoke(cli_root, ["run"])

    assert write_grants == ["projects"]
