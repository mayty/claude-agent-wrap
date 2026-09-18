# This file has been edited with the assistance of an AI tool.
"""Tests for CLI dispatch — guards against help/registration drift."""

from typing import TYPE_CHECKING

import click
import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli import command_groups
from agent_wrap.cli.secrets.run import secrets_group

if TYPE_CHECKING:
    from click.testing import CliRunner

#: Width click's help formatter settles on for any terminal 80 columns or wider:
#: `max(min(terminal_columns, max_content_width=80) - 2, 50)`.
ROOT_HELP_WIDTH = 78

#: Every verb `agent` is expected to expose. Spelled out rather than derived so that
#: adding or removing one is a deliberate edit here, not a silent consequence.
EXPECTED_VERBS = frozenset(
    {
        "cleanup",
        "create",
        "inspect",
        "logs",
        "rebuild",
        "reindex",
        "run",
        "secrets",
        "stats",
        "update",
    }
)


def test_registered_verbs_match_the_expected_set() -> None:
    assert {command.name for command in command_groups} == EXPECTED_VERBS


def test_root_group_registers_every_command_group() -> None:
    assert set(cli_root.commands) == EXPECTED_VERBS


def test_every_verb_has_a_summary_that_is_not_truncated(subtests: pytest.Subtests) -> None:
    """
    No command sets ``short_help``; click derives it from the docstring's first paragraph.

    A derived summary too long for the commands listing is silently truncated with an
    ellipsis, so this is the guard that used to be the explicit ``short_help`` kwarg.
    """
    limit = ROOT_HELP_WIDTH - 6 - max(len(name) for name in EXPECTED_VERBS)
    for command in command_groups:
        with subtests.test(msg=str(command.name)):
            assert isinstance(command, click.Command)
            assert command.short_help is None, f"{command.name} sets short_help by hand"
            summary = command.get_short_help_str(limit)
            assert summary, f"{command.name} has no summary"
            assert not summary.endswith("..."), f"{command.name} summary is truncated: {summary}"


def test_every_secrets_subcommand_has_a_summary_that_is_not_truncated(
    subtests: pytest.Subtests,
) -> None:
    limit = ROOT_HELP_WIDTH - 6 - max(len(name) for name in secrets_group.commands)
    for name, command in secrets_group.commands.items():
        with subtests.test(msg=name):
            summary = command.get_short_help_str(limit)
            assert summary, f"secrets {name} has no summary"
            assert not summary.endswith("..."), f"secrets {name} summary is truncated: {summary}"


def test_help_lists_every_registered_command(runner: CliRunner, subtests: pytest.Subtests) -> None:
    result = runner.invoke(cli_root, ["--help"])
    assert result.exit_code == 0
    for name in sorted(EXPECTED_VERBS):
        with subtests.test(msg=name):
            assert name in result.output


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_root_help_flags_exit_zero(runner: CliRunner, flag: str) -> None:
    """``-h`` is not a click default; the root group declares it for every subcommand."""
    result = runner.invoke(cli_root, [flag])
    assert result.exit_code == 0
    assert "Usage: agent" in result.output


def test_no_arguments_is_a_usage_error(runner: CliRunner) -> None:
    """Click's own no_args_is_help: the help text, but as a usage error."""
    result = runner.invoke(cli_root, [])
    assert result.exit_code == 2
    assert "Usage: agent" in result.output


def test_unknown_command_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["no-such-cmd"])
    assert result.exit_code == 2
    assert "No such command 'no-such-cmd'" in result.output


def test_mistyped_command_is_suggested(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["stat"])
    assert result.exit_code == 2
    assert "Did you mean 'stats'?" in result.output


def test_verb_completion_offers_every_command() -> None:
    ctx = click.Context(cli_root, info_name="agent")
    offered = {item.value for item in cli_root.shell_complete(ctx, "")}
    assert offered == EXPECTED_VERBS


def test_verb_completion_filters_by_prefix() -> None:
    ctx = click.Context(cli_root, info_name="agent")
    offered = {item.value for item in cli_root.shell_complete(ctx, "s")}
    assert offered == {"secrets", "stats"}
