# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.secrets — argument parsing and calling protocol."""

import contextlib
import io
from typing import TYPE_CHECKING

import click
import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli.secrets.run import secrets_group
from agent_wrap.constants import TELEGRAM_SIDECAR_NAME
from agent_wrap.containers import services
from agent_wrap.domain.secrets.models import SecretEntry, SecretsCheckReport, SecretsSetResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import Mock

    from click.testing import CliRunner
    from pytest_mock import MockerFixture

    from agent_wrap.domain.display.service import DisplayService

#: The four subcommands the group must expose, and nothing else.
SUBCOMMANDS = ("check", "set", "clear", "cleanup")


@pytest.fixture
def display_mock_service(non_tty_display: DisplayService) -> Mock:
    """Return the mocked DisplayService with the real table renderer wired in."""
    dsp = services.display_service
    dsp.render_table.side_effect = non_tty_display.render_table  # pyrefly: ignore
    return dsp


@pytest.fixture
def stdout(display_mock_service: Mock, non_tty_display: DisplayService) -> Callable[[], list[str]]:
    """
    Return everything the command put on the terminal, with its table drawn.

    `mock_calls` rather than the per-method lists, so a table keeps its place among the
    prose lines either side of it.
    """

    def _stdout() -> list[str]:
        out: list[str] = []
        for name, args, _kwargs in display_mock_service.mock_calls:
            if name == "show":
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    non_tty_display.show(args[0])
                out.extend(buffer.getvalue().splitlines())
            elif name in {"info", "success", "error", "warning"} and args:
                out.append(str(args[0]))
        return out

    return _stdout


@pytest.fixture
def row_cells(stdout: Callable[[], list[str]]) -> Callable[[str], list[str]]:
    """Return the drawn row for *key* as stripped cells, the cell-level assertion seam."""

    def _row_cells(key: str) -> list[str]:
        line = next(line for line in stdout() if f" {key} " in line)
        return [cell.strip() for cell in line.strip().strip("│").split("│")]

    return _row_cells


def test_group_exposes_exactly_the_four_subcommands() -> None:
    assert set(secrets_group.commands) == set(SUBCOMMANDS)


def test_bare_secrets_is_a_usage_error(runner: CliRunner) -> None:
    """A group invoked with no subcommand prints its help and exits 2."""
    result = runner.invoke(cli_root, ["secrets"])
    assert result.exit_code == 2
    for name in SUBCOMMANDS:
        assert name in result.output


def test_unknown_subcommand_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["secrets", "bogus"])
    assert result.exit_code == 2
    assert "No such command 'bogus'" in result.output


def test_mistyped_subcommand_is_suggested(runner: CliRunner) -> None:
    """Click suggests near-misses, which argparse's suggest_on_error did for choices=."""
    result = runner.invoke(cli_root, ["secrets", "chek"])
    assert result.exit_code == 2
    assert "Did you mean 'check'?" in result.output


@pytest.mark.parametrize("action", ["check", "set", "clear"])
def test_sidecar_is_required(runner: CliRunner, action: str) -> None:
    """Click enforces the arity these three share; no hand-rolled check remains."""
    result = runner.invoke(cli_root, ["secrets", action])
    assert result.exit_code == 2
    assert "Missing argument 'SIDECAR'" in result.output


def test_cleanup_takes_no_sidecar(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["secrets", "cleanup", "extra"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output
    services.secrets_service.cleanup_secrets.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_check_reports_all_present(runner: CliRunner) -> None:
    """'check telegram' returns 0 when the report says every secret is present."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={
            "telegram:TelegramBotToken": SecretEntry(present=True, length=46, hint="12***bC"),
            "telegram:TelegramChatId": SecretEntry(present=True, length=10, hint="12***90"),
        },
        all_present=True,
        declares_none=False,
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    services.secrets_service.check_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]


def test_check_reports_length_and_hint_for_present_rows(
    runner: CliRunner, row_cells: Callable[[str], list[str]]
) -> None:
    """The length and the masked hint are what make a doubled paste visible."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:TelegramBotToken": SecretEntry(present=True, length=46, hint="12***bC")},
        all_present=True,
        declares_none=False,
    )

    runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])

    assert row_cells("telegram:TelegramBotToken") == [
        "telegram:TelegramBotToken",
        "OK",
        "46",
        "12***bC",
    ]


def test_check_heads_its_columns_and_keeps_declaration_order(
    runner: CliRunner, stdout: Callable[[], list[str]]
) -> None:
    """The report's order is its contract; the table names the columns rather than sorting."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={
            "telegram:TelegramChatId": SecretEntry(present=False),
            "telegram:TelegramBotToken": SecretEntry(present=True, length=46, hint="12***bC"),
        },
        all_present=False,
        declares_none=False,
    )

    runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])

    out = stdout()
    assert f"Secrets for '{TELEGRAM_SIDECAR_NAME}':" in out
    header = next(line for line in out if "SECRET" in line)
    assert [cell.strip() for cell in header.strip().strip("│").split("│")] == [
        "SECRET",
        "STATE",
        "LENGTH",
        "HINT",
    ]
    rendered = "\n".join(out)
    assert rendered.index("telegram:TelegramChatId") < rendered.index("telegram:TelegramBotToken")


def test_check_renders_an_empty_stored_value_apart_from_a_missing_one(
    runner: CliRunner, row_cells: Callable[[str], list[str]]
) -> None:
    """A secret stored as "" is present at length 0; blanking it would read as MISSING."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:TelegramBotToken": SecretEntry(present=True, length=0, hint="***")},
        all_present=True,
        declares_none=False,
    )

    runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])

    assert row_cells("telegram:TelegramBotToken") == ["telegram:TelegramBotToken", "OK", "0", "***"]


def test_check_missing_secret_returns_one(
    runner: CliRunner, stdout: Callable[[], list[str]]
) -> None:
    """'check' returns 1 when the report's verdict is not all-present."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:Token": SecretEntry(present=False)},
        all_present=False,
        declares_none=False,
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 1
    rendered = "\n".join(stdout())
    assert f"1 of 1 secrets missing for '{TELEGRAM_SIDECAR_NAME}'" in rendered
    assert f"agent secrets set {TELEGRAM_SIDECAR_NAME}" in rendered


def test_check_missing_row_carries_no_length_or_hint(
    runner: CliRunner, row_cells: Callable[[str], list[str]]
) -> None:
    """An absent key has nothing to describe, and must not be rendered as a zero-length one."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:Token": SecretEntry(present=False)},
        all_present=False,
        declares_none=False,
    )

    runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])

    assert row_cells("telegram:Token") == ["telegram:Token", "MISSING", "", ""]


def test_check_declares_no_secrets_returns_zero(runner: CliRunner) -> None:
    """A sidecar requiring nothing is reported as such, not as an empty table."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={}, all_present=True, declares_none=True
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    assert "declares no secrets" in services.display_service.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    services.display_service.show.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_set_non_interactive_returns_one(runner: CliRunner) -> None:
    """'set' reports the result's error and fails when there is no TTY."""
    services.secrets_service.set_secrets.return_value = SecretsSetResult(  # pyrefly: ignore [missing-attribute]
        keys_set=[], error="Cannot prompt for secrets in a non-interactive session."
    )

    result = runner.invoke(cli_root, ["secrets", "set", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 1
    services.secrets_service.set_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "Cannot prompt for secrets in a non-interactive session."
    )


def test_set_succeeds_when_keys_were_set(runner: CliRunner) -> None:
    services.secrets_service.set_secrets.return_value = SecretsSetResult(  # pyrefly: ignore [missing-attribute]
        keys_set=["telegram:Token"]
    )

    result = runner.invoke(cli_root, ["secrets", "set", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    services.display_service.error.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_clear_removes_namespaced_keys(runner: CliRunner) -> None:
    """'clear telegram' delegates to secrets_service.clear_secrets."""
    services.secrets_service.clear_secrets.return_value = ["telegram:Token"]  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "clear", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    services.secrets_service.clear_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]


def test_clear_tabulates_every_removed_key(
    runner: CliRunner, stdout: Callable[[], list[str]], row_cells: Callable[[str], list[str]]
) -> None:
    """The title names the sidecar and the count; one row states each key's fate."""
    services.secrets_service.clear_secrets.return_value = [  # pyrefly: ignore [missing-attribute]
        "telegram:TelegramBotToken",
        "telegram:TelegramChatId",
    ]

    runner.invoke(cli_root, ["secrets", "clear", TELEGRAM_SIDECAR_NAME])

    assert f"Removed from '{TELEGRAM_SIDECAR_NAME}' (2):" in stdout()
    assert row_cells("telegram:TelegramChatId") == ["telegram:TelegramChatId", "REMOVED"]


def test_cleanup_removes_unknown_keys(runner: CliRunner) -> None:
    """'cleanup' delegates to secrets_service.cleanup_secrets."""
    services.secrets_service.cleanup_secrets.return_value = ["orphan:Key"]  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "cleanup"])
    assert result.exit_code == 0
    services.secrets_service.cleanup_secrets.assert_called_once()  # pyrefly: ignore [missing-attribute]


def test_cleanup_tabulates_every_removed_key(
    runner: CliRunner, stdout: Callable[[], list[str]], row_cells: Callable[[str], list[str]]
) -> None:
    """Its own title says the keys belonged to no known sidecar, so the row need not."""
    services.secrets_service.cleanup_secrets.return_value = ["orphan:Key"]  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["secrets", "cleanup"])

    assert "Unknown keys removed (1):" in stdout()
    assert row_cells("orphan:Key") == ["orphan:Key", "REMOVED"]


@pytest.mark.parametrize(
    ("argv", "method", "expected"),
    [
        (["secrets", "clear", TELEGRAM_SIDECAR_NAME], "clear_secrets", "No secrets found"),
        (["secrets", "cleanup"], "cleanup_secrets", "No unknown keys found."),
    ],
)
def test_nothing_removed_prints_prose_and_no_table(
    runner: CliRunner, argv: list[str], method: str, expected: str
) -> None:
    """An empty table is a header over nothing; the prose line says it in one line."""
    getattr(services.secrets_service, method).return_value = []

    result = runner.invoke(cli_root, argv)
    assert result.exit_code == 0
    assert expected in services.display_service.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    services.display_service.show.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.fixture
def sidecar_param(mocker: MockerFixture) -> click.Parameter:
    """Return the ``<sidecar>`` argument of ``secrets check``, sidecar list stubbed."""
    mocker.patch.object(
        services.secrets_service,
        "known_sidecars",
        return_value=["litellm-bedrock", TELEGRAM_SIDECAR_NAME],
    )
    check = secrets_group.commands["check"]
    return next(param for param in check.params if param.name == "sidecar")


@pytest.mark.parametrize(
    ("incomplete", "expected"),
    [
        ("", ["litellm-bedrock", TELEGRAM_SIDECAR_NAME]),
        ("lite", ["litellm-bedrock"]),
        ("tele", [TELEGRAM_SIDECAR_NAME]),
        ("zzz", []),
    ],
)
def test_sidecar_argument_completes_known_sidecars(
    sidecar_param: click.Parameter, incomplete: str, expected: list[str]
) -> None:
    """The one dynamic completion the wrapper has, now a click shell_complete callback."""
    ctx = click.Context(secrets_group.commands["check"], info_name="check")
    offered = [item.value for item in sidecar_param.shell_complete(ctx, incomplete)]
    assert offered == expected
