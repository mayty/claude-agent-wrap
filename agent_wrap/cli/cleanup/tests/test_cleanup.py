# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.cleanup — parsing, prompting, and reporting."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli.cleanup.constants import CLEANUP_LABEL
from agent_wrap.containers import services
from agent_wrap.domain.build.models import ImageCleanupOutcome, ImageCleanupScope
from agent_wrap.domain.stats.models import CleanupOutcome, CleanupResult, CleanupScope

if TYPE_CHECKING:
    from unittest.mock import Mock

    from click.testing import CliRunner

_ORPHANED = [Path("/wrap/litellm-logs/hashA"), Path("/wrap/litellm-logs/hashB")]
_STALE = [Path("/gone/project")]


def _scope(
    *,
    orphaned: list[Path] | None = None,
    stale: list[Path] | None = None,
    freed_estimate: int = 3_145_728,
) -> CleanupScope:
    return CleanupScope(
        orphaned_dirs=list(_ORPHANED) if orphaned is None else orphaned,
        stale_paths=list(_STALE) if stale is None else stale,
        freed_estimate=freed_estimate,
    )


def _outcome(*, finalized: bool = True, removed_paths: list[Path] | None = None) -> CleanupOutcome:
    return CleanupOutcome(
        result=CleanupResult(
            removed=2,
            freed_bytes=2_097_152,
            archive_path=Path("/wrap/.agent-launches/orphaned-usage-archive.json"),
            staging_path=Path("/wrap/.agent-launches/orphaned-usage-archive.new.json"),
            finalized=finalized,
        ),
        removed_paths=list(_STALE) if removed_paths is None else removed_paths,
    )


@pytest.fixture
def stats_mock() -> Mock:
    """Return the mocked StatsService, pre-seeded with a scope and a successful run."""
    stats = services.stats_service
    stats.cleanup_scope.return_value = _scope()  # pyrefly: ignore [missing-attribute]
    stats.run_cleanup.return_value = _outcome()  # pyrefly: ignore [missing-attribute]
    return stats


@pytest.fixture(autouse=True)
def build_mock() -> Mock:
    """
    Return the mocked BuildService, seeded as a host with no outdated images.

    Autouse so the log-and-registry cases in this file describe exactly what they did
    before images joined the command: every one of them runs against an empty image
    scope. The image behaviour has its own file.
    """
    build = services.build_service
    empty_scope = ImageCleanupScope(images=[], unattributable=0)
    build.image_cleanup_scope.return_value = empty_scope  # pyrefly: ignore [missing-attribute]
    build.remove_images.return_value = ImageCleanupOutcome(  # pyrefly: ignore [missing-attribute]
        removed=[], skipped=[]
    )
    return build


@pytest.fixture
def display_mock_service() -> Mock:
    """Return the mocked DisplayService, with formatters producing marked strings."""
    dsp = services.display_service
    dsp.format_bytes.side_effect = lambda n: f"<{n}B>"  # pyrefly: ignore [missing-attribute]
    dsp.spin_while.side_effect = lambda **kw: kw["work"]()  # pyrefly: ignore [missing-attribute]
    return dsp


def _stdout(dsp: Mock) -> str:
    """Join every info/success/error message the command emitted."""
    calls = [
        *dsp.info.call_args_list,
        *dsp.success.call_args_list,
        *dsp.error.call_args_list,
    ]
    return "\n".join(str(c[0][0]) for c in calls if c[0])


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_zero(runner: CliRunner, flag: str) -> None:
    result = runner.invoke(cli_root, ["cleanup", flag])
    assert result.exit_code == 0
    assert "--dry-run" in result.output


def test_unknown_flag_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["cleanup", "--bogus"])
    assert result.exit_code == 2
    assert "No such option '--bogus'" in result.output


def test_positional_argument_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["cleanup", "stray"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output


@pytest.mark.usefixtures("stats_mock")
def test_empty_scope_skips_prompt(runner: CliRunner, display_mock_service: Mock) -> None:
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], stale=[])  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "Nothing to clean up" in _stdout(display_mock_service)
    display_mock_service.prompt_confirm.assert_not_called()
    services.stats_service.run_cleanup.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock")
def test_dry_run_reports_without_prompting(runner: CliRunner, display_mock_service: Mock) -> None:
    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0

    out = _stdout(display_mock_service)
    assert "2 project log(s) will be deleted" in out
    assert "<3145728B>" in out
    assert "1 stale project registry entr(y/ies)" in out
    display_mock_service.prompt_confirm.assert_not_called()


@pytest.mark.usefixtures("stats_mock", "display_mock_service")
def test_dry_run_never_mutates(runner: CliRunner) -> None:
    runner.invoke(cli_root, ["cleanup", "--dry-run"])
    services.stats_service.run_cleanup.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock")
def test_shows_summary_before_prompting(runner: CliRunner, display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = False
    runner.invoke(cli_root, ["cleanup"])

    out = _stdout(display_mock_service)
    assert "2 project log(s) will be deleted" in out
    assert "<3145728B>" in out
    display_mock_service.prompt_confirm.assert_called_once()


@pytest.mark.usefixtures("stats_mock")
def test_declining_skips_the_cleanup(runner: CliRunner, display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = False

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "Cleanup cancelled." in _stdout(display_mock_service)
    services.stats_service.run_cleanup.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock")
def test_confirming_acts_on_the_surveyed_scope(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """The confirmed run must act on the very scope the summary described."""
    display_mock_service.prompt_confirm.return_value = True
    scope = _scope()
    services.stats_service.cleanup_scope.return_value = scope  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    services.stats_service.run_cleanup.assert_called_once_with(scope)  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock")
def test_success_message_reports_actual_freed_bytes(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """The summary must report what was freed, not the pre-confirmation estimate."""
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = _stdout(display_mock_service)
    assert "2 project log(s) deleted" in out
    assert "<2097152B>" in out
    assert "<3145728B>" not in display_mock_service.success.call_args[0][0]
    assert "1 stale registry entr(y/ies) removed" in out


@pytest.mark.usefixtures("stats_mock")
def test_unfinalized_archive_reports_manual_fallback(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    display_mock_service.prompt_confirm.return_value = True
    services.stats_service.run_cleanup.return_value = _outcome(finalized=False, removed_paths=[])  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 1
    message = display_mock_service.error.call_args[0][0]
    assert "failed to finalize" in message
    assert "mv /wrap/.agent-launches/orphaned-usage-archive.new.json" in message
    assert "/wrap/.agent-launches/orphaned-usage-archive.json" in message


@pytest.mark.usefixtures("stats_mock")
def test_runs_with_only_stale_entries(runner: CliRunner, display_mock_service: Mock) -> None:
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], freed_estimate=0)  # pyrefly: ignore [missing-attribute]
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = _stdout(display_mock_service)
    assert "0 project log(s) will be deleted" in out
    assert "<0B>" in out


@pytest.mark.usefixtures("stats_mock")
def test_omits_stale_line_when_none(runner: CliRunner, display_mock_service: Mock) -> None:
    services.stats_service.cleanup_scope.return_value = _scope(stale=[])  # pyrefly: ignore [missing-attribute]
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "stale project registry" not in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock")
def test_scope_spinner_runs_before_empty_scope_check(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """The scan spinner must run even when there is nothing to clean up."""
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], stale=[])  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    display_mock_service.spin_while.assert_called_once()
    call = display_mock_service.spin_while.call_args
    assert call.kwargs["label"] == CLEANUP_LABEL
    assert call.kwargs["message"] == "scanning…"
    assert call.kwargs["done_message"]() is None
    assert callable(call.kwargs["work"])


@pytest.mark.usefixtures("stats_mock")
def test_scope_spinner_runs_on_dry_run(runner: CliRunner, display_mock_service: Mock) -> None:
    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    assert display_mock_service.spin_while.call_count == 1
    assert display_mock_service.spin_while.call_args[1]["label"] == CLEANUP_LABEL
    assert display_mock_service.spin_while.call_args[1]["message"] == "scanning…"


@pytest.mark.usefixtures("stats_mock")
def test_cleanup_spinner_not_run_on_decline(runner: CliRunner, display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = False

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert display_mock_service.spin_while.call_count == 1


@pytest.mark.usefixtures("stats_mock")
def test_cleanup_spinner_runs_after_confirmation(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert display_mock_service.spin_while.call_count == 2
    second_call = display_mock_service.spin_while.call_args_list[1]
    assert second_call.kwargs["label"] == CLEANUP_LABEL
    assert second_call.kwargs["message"] == "cleaning up…"
    services.stats_service.run_cleanup.assert_called_once()  # pyrefly: ignore [missing-attribute]
