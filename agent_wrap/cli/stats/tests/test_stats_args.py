# This file has been created with the assistance of an AI tool.
"""
Tests for `agent stats` argument parsing.

Only the parsing layer lives here — flag spellings, the relative ``-Nd`` date form,
per-value validation, and how a rejected window is reported. The resolution table
itself belongs to ``StatsService.resolve_window`` and is tested in the stats domain.
Rendering lives in ``test_stats.py``.
"""

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli import params
from agent_wrap.containers import services
from agent_wrap.domain.stats.models import WindowError

if TYPE_CHECKING:
    from click.testing import CliRunner
    from pytest_mock import MockerFixture

#: A fixed "today" so relative offsets are deterministic.
TODAY = date(2026, 6, 29)


@pytest.fixture(autouse=True)
def _frozen_today(mocker: MockerFixture) -> None:
    """Freeze "today" and pin the day boundary so ``-Nd`` is plain UTC-date arithmetic."""
    services.stats_service.now_utc.return_value = datetime(  # pyrefly: ignore [missing-attribute]
        TODAY.year, TODAY.month, TODAY.day, 12, 0, 0, tzinfo=UTC
    )
    mocker.patch.object(params, "DAY_START_HOURS", 0)
    services.stats_service.resolve_window.return_value = ("lo", "hi")  # pyrefly: ignore [missing-attribute]
    services.config_service.read_project_paths.return_value = []  # pyrefly: ignore [missing-attribute]


def resolve_call() -> tuple[date | None, date | None, int | None, bool]:
    """Return the (from, until, days, days_given) the command handed the resolver."""
    call = services.stats_service.resolve_window.call_args  # pyrefly: ignore [missing-attribute]
    return (*call.args, call.kwargs["days_given"])


def test_no_flags_resolve_to_an_unbounded_request(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["stats"])
    assert result.exit_code == 0
    assert resolve_call() == (None, None, None, False)


def test_absolute_dates_reach_the_resolver(runner: CliRunner) -> None:
    runner.invoke(cli_root, ["stats", "--from", "2026-06-01", "--until", "2026-06-10"])
    assert resolve_call() == (date(2026, 6, 1), date(2026, 6, 10), None, False)


def test_short_flags_are_equivalent(runner: CliRunner) -> None:
    runner.invoke(cli_root, ["stats", "-f", "2026-06-01", "-u", "2026-06-10", "-d", "5"])
    assert resolve_call() == (date(2026, 6, 1), date(2026, 6, 10), 5, True)


@pytest.mark.parametrize("spelling", ["--from=-14d", "-f-14d"])
def test_relative_date_in_the_attached_value_form(runner: CliRunner, spelling: str) -> None:
    """``--long=value`` and the glued ``-svalue`` short form both carry a leading dash."""
    runner.invoke(cli_root, ["stats", spelling])
    assert resolve_call() == (TODAY - timedelta(days=14), None, None, False)


@pytest.mark.parametrize("flag", ["--from", "-f"])
def test_relative_date_needs_no_equals(runner: CliRunner, flag: str) -> None:
    """Click takes the next token as the value, so ``--from -14d`` needs no rewriting."""
    runner.invoke(cli_root, ["stats", flag, "-14d"])
    assert resolve_call() == (TODAY - timedelta(days=14), None, None, False)


def test_relative_until_is_offset_from_today(runner: CliRunner) -> None:
    runner.invoke(cli_root, ["stats", "--until", "-7d", "--days", "3"])
    assert resolve_call() == (None, TODAY - timedelta(days=7), 3, True)


def test_days_zero_is_given_not_absent(runner: CliRunner) -> None:
    """``--days 0`` must reach the resolver as days_given, or it reads as "no flag"."""
    runner.invoke(cli_root, ["stats", "--days", "0"])
    assert resolve_call() == (None, None, 0, True)


@pytest.fixture
def one_project(mocker: MockerFixture) -> None:
    """Seed one project and stub rendering, so the command reaches build_report."""
    services.config_service.read_project_paths.return_value = ["/proj"]  # pyrefly: ignore [missing-attribute]
    mocker.patch("agent_wrap.cli.stats.run.render", return_value="")
    mocker.patch("agent_wrap.cli.stats.run.render_source_breakdown", return_value=None)


@pytest.mark.usefixtures("one_project")
@pytest.mark.parametrize(
    ("flags", "expected"), [([], False), (["-v"], True), (["--verbose"], True)]
)
def test_verbose_flag_reaches_the_report(
    runner: CliRunner, flags: list[str], *, expected: bool
) -> None:
    runner.invoke(cli_root, ["stats", *flags])
    _projects, args = services.stats_service.build_report.call_args.args  # pyrefly: ignore [missing-attribute]
    assert args.verbose is expected


@pytest.mark.usefixtures("one_project")
@pytest.mark.parametrize(
    ("flags", "expected"), [([], False), (["-r"], True), (["--refresh"], True)]
)
def test_refresh_flag_reaches_the_report(
    runner: CliRunner, flags: list[str], *, expected: bool
) -> None:
    runner.invoke(cli_root, ["stats", *flags])
    _projects, args = services.stats_service.build_report.call_args.args  # pyrefly: ignore [missing-attribute]
    assert args.refresh is expected


def test_window_error_is_a_usage_error(runner: CliRunner) -> None:
    services.stats_service.resolve_window.return_value = WindowError("nope")  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["stats", "-d", "3"])
    assert result.exit_code == 2
    assert "nope" in result.output
    services.config_service.read_project_paths.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("value", ["june-first", "2026-13-01", "14d"])
def test_malformed_from_is_rejected(runner: CliRunner, value: str) -> None:
    result = runner.invoke(cli_root, ["stats", "--from", value])
    assert result.exit_code == 2
    assert "expects YYYY-MM-DD or -Nd" in result.output
    services.stats_service.resolve_window.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_negative_days_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["stats", "--days", "-3"])
    assert result.exit_code == 2
    assert "is not in the range" in result.output


def test_non_integer_days_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["stats", "--days", "abc"])
    assert result.exit_code == 2
    assert "is not a valid integer range" in result.output


def test_bare_days_reports_its_missing_value(runner: CliRunner) -> None:
    """A trailing flag with no value must say so rather than be silently absorbed."""
    result = runner.invoke(cli_root, ["stats", "--days"])
    assert result.exit_code == 2
    assert "requires an argument" in result.output


def test_unknown_flag_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["stats", "--nope"])
    assert result.exit_code == 2
    assert "No such option '--nope'" in result.output


def test_positional_argument_is_rejected(runner: CliRunner) -> None:
    """The registry path is derived, never passed — a stray positional is an error."""
    result = runner.invoke(cli_root, ["stats", "/some/projects.txt"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output


@pytest.mark.usefixtures("one_project")
@pytest.mark.parametrize("flag", ["--pattern", "-p"])
def test_pattern_is_compiled_and_forwarded(runner: CliRunner, flag: str) -> None:
    runner.invoke(cli_root, ["stats", flag, "^(proj-a|proj-b)$"])
    _projects, args = services.stats_service.build_report.call_args.args  # pyrefly: ignore [missing-attribute]
    assert args.pattern is not None
    assert args.pattern.pattern == "^(proj-a|proj-b)$"


@pytest.mark.usefixtures("one_project")
def test_pattern_defaults_to_none(runner: CliRunner) -> None:
    runner.invoke(cli_root, ["stats"])
    _projects, args = services.stats_service.build_report.call_args.args  # pyrefly: ignore [missing-attribute]
    assert args.pattern is None


def test_invalid_regex_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["stats", "-p", "[unclosed"])
    assert result.exit_code == 2
    assert "invalid regex pattern" in result.output
    services.stats_service.resolve_window.assert_not_called()  # pyrefly: ignore [missing-attribute]
