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

#: A two-sidecar sweep with something missing on each, so the verdict has to count both.
SWEEP_REPORTS = {
    "litellm-bedrock": SecretsCheckReport(
        entries={
            "litellm-bedrock:api_key": SecretEntry(present=True, length=46, hint="12***bC"),
            "litellm-bedrock:region": SecretEntry(present=False),
        },
        all_present=False,
        declares_none=False,
    ),
    TELEGRAM_SIDECAR_NAME: SecretsCheckReport(
        entries={
            "telegram:TelegramBotToken": SecretEntry(present=False),
            "telegram:TelegramChatId": SecretEntry(present=False),
        },
        all_present=False,
        declares_none=False,
    ),
}


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


@pytest.mark.parametrize("action", ["set", "clear"])
def test_sidecar_is_required_for_set_and_clear(runner: CliRunner, action: str) -> None:
    """Click enforces the arity these two share; no hand-rolled check remains."""
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

    assert row_cells("TelegramBotToken") == [
        TELEGRAM_SIDECAR_NAME,
        "TelegramBotToken",
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
    assert "Secrets:" in out
    header = next(line for line in out if "SIDECAR" in line)
    assert [cell.strip() for cell in header.strip().strip("│").split("│")] == [
        "SIDECAR",
        "SECRET",
        "STATE",
        "LENGTH",
        "HINT",
    ]
    rendered = "\n".join(out)
    assert rendered.index("TelegramChatId") < rendered.index("TelegramBotToken")


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

    assert row_cells("TelegramBotToken") == [
        TELEGRAM_SIDECAR_NAME,
        "TelegramBotToken",
        "OK",
        "0",
        "***",
    ]


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

    assert row_cells("Token") == [TELEGRAM_SIDECAR_NAME, "Token", "MISSING", "", ""]


def test_check_declares_no_secrets_returns_zero(runner: CliRunner) -> None:
    """A sidecar requiring nothing is reported as such, not as an empty table."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={}, all_present=True, declares_none=True
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    assert "declares no secrets" in services.display_service.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    services.display_service.show.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_check_sidecar_is_optional() -> None:
    """Only `check` may be run without a name; `set` and `clear` keep click's arity."""
    required = {
        action: next(
            param for param in secrets_group.commands[action].params if param.name == "sidecar"
        ).required
        for action in ("check", "set", "clear")
    }
    assert required == {"check": False, "set": True, "clear": True}


def test_check_rejects_a_second_sidecar(runner: CliRunner) -> None:
    """Optional is not variadic: click still takes at most one name."""
    result = runner.invoke(cli_root, ["secrets", "check", "a", "b"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output


def test_check_without_a_sidecar_sweeps_every_sidecar(runner: CliRunner) -> None:
    """A bare `check` asks for the whole registry, not for one unnamed sidecar."""
    services.secrets_service.check_all_sidecars.return_value = {}  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 0
    services.secrets_service.check_all_sidecars.assert_called_once_with()  # pyrefly: ignore [missing-attribute]
    services.secrets_service.check_secrets.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_check_with_a_sidecar_does_not_sweep(runner: CliRunner) -> None:
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={}, all_present=True, declares_none=True
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])

    assert result.exit_code == 0
    services.secrets_service.check_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]
    services.secrets_service.check_all_sidecars.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_check_uses_one_table_shape_in_both_modes(
    runner: CliRunner, stdout: Callable[[], list[str]]
) -> None:
    """A named run is a one-entry mapping through the same builder, so the headers agree."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:Token": SecretEntry(present=True, length=4, hint="***")},
        all_present=True,
        declares_none=False,
    )
    services.secrets_service.check_all_sidecars.return_value = SWEEP_REPORTS  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    runner.invoke(cli_root, ["secrets", "check"])

    headers = [
        [cell.strip() for cell in line.strip().strip("│").split("│")]
        for line in stdout()
        if "SIDECAR" in line
    ]
    assert headers == [["SIDECAR", "SECRET", "STATE", "LENGTH", "HINT"]] * 2


def test_check_all_shows_the_bare_key_under_the_sidecar_column(
    runner: CliRunner, row_cells: Callable[[str], list[str]], stdout: Callable[[], list[str]]
) -> None:
    """SIDECAR carries the namespace, so SECRET must not spell it in again as a prefix."""
    services.secrets_service.check_all_sidecars.return_value = SWEEP_REPORTS  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 1
    assert row_cells("api_key") == ["litellm-bedrock", "api_key", "OK", "46", "12***bC"]
    assert row_cells("region") == ["litellm-bedrock", "region", "MISSING", "", ""]
    assert "litellm-bedrock:api_key" not in "\n".join(stdout())


def test_check_all_groups_in_sidecar_name_order(
    runner: CliRunner, stdout: Callable[[], list[str]]
) -> None:
    """The service's order is the table's order; the CLI must not sort again."""
    services.secrets_service.check_all_sidecars.return_value = SWEEP_REPORTS  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["secrets", "check"])

    rendered = "\n".join(stdout())
    assert rendered.index("litellm-bedrock") < rendered.index(TELEGRAM_SIDECAR_NAME)
    assert rendered.index("TelegramBotToken") < rendered.index("TelegramChatId")


def test_check_all_names_the_failing_sidecars_in_one_line(runner: CliRunner) -> None:
    """One diagnostic: the arithmetic per sidecar, then one instruction."""
    services.secrets_service.check_all_sidecars.return_value = SWEEP_REPORTS  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 1
    verdict = services.display_service.error.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert verdict.startswith(
        f"3 secrets missing across 2 sidecars: litellm-bedrock, {TELEGRAM_SIDECAR_NAME}"
    )
    assert verdict.endswith("Run 'agent secrets set <sidecar>' to set them.")


def test_check_all_inflects_a_single_missing_secret(runner: CliRunner) -> None:
    """'1 secret missing across 1 sidecar' is a sentence; the plural form is not."""
    services.secrets_service.check_all_sidecars.return_value = {  # pyrefly: ignore [missing-attribute]
        TELEGRAM_SIDECAR_NAME: SecretsCheckReport(
            entries={"telegram:Token": SecretEntry(present=False)},
            all_present=False,
            declares_none=False,
        )
    }

    runner.invoke(cli_root, ["secrets", "check"])

    verdict = services.display_service.error.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert verdict.startswith(f"1 secret missing across 1 sidecar: {TELEGRAM_SIDECAR_NAME}")


def test_check_all_does_not_name_a_sidecar_that_is_complete(runner: CliRunner) -> None:
    services.secrets_service.check_all_sidecars.return_value = {  # pyrefly: ignore [missing-attribute]
        "litellm-bedrock": SecretsCheckReport(
            entries={
                "litellm-bedrock:api_key": SecretEntry(present=True, length=46, hint="12***bC")
            },
            all_present=True,
            declares_none=False,
        ),
        TELEGRAM_SIDECAR_NAME: SecretsCheckReport(
            entries={"telegram:Token": SecretEntry(present=False)},
            all_present=False,
            declares_none=False,
        ),
    }

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 1
    verdict = services.display_service.error.call_args[0][0]  # pyrefly: ignore [missing-attribute]
    assert verdict.startswith(f"1 secret missing across 1 sidecar: {TELEGRAM_SIDECAR_NAME}")
    assert "litellm-bedrock" not in verdict


def test_check_all_returns_zero_when_every_sidecar_is_complete(runner: CliRunner) -> None:
    services.secrets_service.check_all_sidecars.return_value = {  # pyrefly: ignore [missing-attribute]
        TELEGRAM_SIDECAR_NAME: SecretsCheckReport(
            entries={"telegram:Token": SecretEntry(present=True, length=4, hint="***")},
            all_present=True,
            declares_none=False,
        )
    }

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 0
    services.display_service.error.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_check_all_names_the_sidecars_that_declare_none(
    runner: CliRunner, stdout: Callable[[], list[str]]
) -> None:
    """A known sidecar must not vanish from the report just because it needs nothing."""
    services.secrets_service.check_all_sidecars.return_value = {  # pyrefly: ignore [missing-attribute]
        "litellm-anthropic-sub": SecretsCheckReport(
            entries={}, all_present=True, declares_none=True
        ),
        TELEGRAM_SIDECAR_NAME: SecretsCheckReport(
            entries={"telegram:Token": SecretEntry(present=True, length=4, hint="***")},
            all_present=True,
            declares_none=False,
        ),
    }

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 0
    assert "Declares no secrets: litellm-anthropic-sub." in stdout()
    assert "litellm-anthropic-sub" not in "\n".join(
        line for line in stdout() if "SECRET" in line or "Token" in line
    )


def test_check_all_prints_no_table_when_nothing_is_required(
    runner: CliRunner, stdout: Callable[[], list[str]]
) -> None:
    """A header over no rows reads as a broken table, so the note carries the whole answer."""
    services.secrets_service.check_all_sidecars.return_value = {  # pyrefly: ignore [missing-attribute]
        "litellm-anthropic-sub": SecretsCheckReport(
            entries={}, all_present=True, declares_none=True
        )
    }

    result = runner.invoke(cli_root, ["secrets", "check"])

    assert result.exit_code == 0
    services.display_service.show.assert_not_called()  # pyrefly: ignore [missing-attribute]
    assert "Declares no secrets: litellm-anthropic-sub." in stdout()


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
    assert row_cells("TelegramChatId") == [TELEGRAM_SIDECAR_NAME, "TelegramChatId", "REMOVED"]


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
    assert row_cells("Key") == ["orphan", "Key", "REMOVED"]


def test_removed_table_marks_a_key_with_no_namespace(
    runner: CliRunner, row_cells: Callable[[str], list[str]]
) -> None:
    """A key with no namespace reads as such rather than as an empty cell."""
    services.secrets_service.cleanup_secrets.return_value = ["LegacyToken"]  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["secrets", "cleanup"])

    assert row_cells("LegacyToken") == ["<none>", "LegacyToken", "REMOVED"]


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
