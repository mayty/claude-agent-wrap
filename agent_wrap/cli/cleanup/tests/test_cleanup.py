# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.cleanup — parsing, prompting, and reporting."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.cli.cleanup.complete import complete as cleanup_complete
from agent_wrap.cli.cleanup.constants import CLEANUP_LABEL
from agent_wrap.cli.cleanup.run import build_parser
from agent_wrap.cli.cleanup.run import run as cleanup_run
from agent_wrap.containers import services
from agent_wrap.domain.stats.models import CleanupOutcome, CleanupResult, CleanupScope

if TYPE_CHECKING:
    from unittest.mock import Mock

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
    stats.cleanup_scope.return_value = _scope()  # type: ignore[union-attr]
    stats.run_cleanup.return_value = _outcome()  # type: ignore[union-attr]
    return stats  # type: ignore[return-value]


@pytest.fixture
def display_mock_service() -> Mock:
    """Return the mocked DisplayService, with formatters producing marked strings."""
    dsp = services.display_service
    dsp.format_bytes.side_effect = lambda n: f"<{n}B>"  # type: ignore[union-attr]
    dsp.spin_while.side_effect = lambda **kw: kw["work"]()  # type: ignore[union-attr]
    return dsp  # type: ignore[return-value]


def _stdout(dsp: Mock) -> str:
    """Join every info/success/error message the command emitted."""
    calls = [
        *dsp.info.call_args_list,
        *dsp.success.call_args_list,
        *dsp.error.call_args_list,
    ]
    return "\n".join(str(c[0][0]) for c in calls if c[0])


# --- parsing ---------------------------------------------------------------


def test_parse_dry_run_flag() -> None:
    assert build_parser().parse_args(["--dry-run"]).dry_run is True


def test_parse_defaults_dry_run_false() -> None:
    assert build_parser().parse_args([]).dry_run is False


def test_parse_unknown_arg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--bogus"])
    assert exc.value.code != 0
    assert "unrecognized arguments" in capsys.readouterr().err


def test_run_help_returns_zero() -> None:
    assert cleanup_run(["-h"]) == 0


def test_run_unknown_arg_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cleanup_run(["--bogus"]) == 1
    assert "unrecognized arguments" in capsys.readouterr().err


# --- nothing to do ---------------------------------------------------------


@pytest.mark.usefixtures("stats_mock")
def test_empty_scope_skips_prompt(display_mock_service: Mock) -> None:
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], stale=[])  # type: ignore[union-attr]

    assert cleanup_run([]) == 0
    assert "Nothing to clean up" in _stdout(display_mock_service)
    display_mock_service.prompt_confirm.assert_not_called()
    services.stats_service.run_cleanup.assert_not_called()  # type: ignore[union-attr]


# --- dry run ---------------------------------------------------------------


@pytest.mark.usefixtures("stats_mock")
def test_dry_run_reports_without_prompting(display_mock_service: Mock) -> None:
    assert cleanup_run(["--dry-run"]) == 0

    out = _stdout(display_mock_service)
    assert "2 project log(s) will be deleted" in out
    assert "<3145728B>" in out
    assert "1 stale project registry entr(y/ies)" in out
    display_mock_service.prompt_confirm.assert_not_called()


@pytest.mark.usefixtures("stats_mock", "display_mock_service")
def test_dry_run_never_mutates() -> None:
    cleanup_run(["--dry-run"])
    services.stats_service.run_cleanup.assert_not_called()  # type: ignore[union-attr]


# --- confirmation ----------------------------------------------------------


@pytest.mark.usefixtures("stats_mock")
def test_shows_summary_before_prompting(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = False
    cleanup_run([])

    out = _stdout(display_mock_service)
    assert "2 project log(s) will be deleted" in out
    assert "<3145728B>" in out
    display_mock_service.prompt_confirm.assert_called_once()


@pytest.mark.usefixtures("stats_mock")
def test_declining_skips_the_cleanup(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = False

    assert cleanup_run([]) == 0
    assert "Cleanup cancelled." in _stdout(display_mock_service)
    services.stats_service.run_cleanup.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.usefixtures("stats_mock")
def test_confirming_acts_on_the_surveyed_scope(display_mock_service: Mock) -> None:
    """The confirmed run must act on the very scope the summary described."""
    display_mock_service.prompt_confirm.return_value = True
    scope = _scope()
    services.stats_service.cleanup_scope.return_value = scope  # type: ignore[union-attr]

    assert cleanup_run([]) == 0
    services.stats_service.run_cleanup.assert_called_once_with(scope)  # type: ignore[union-attr]


@pytest.mark.usefixtures("stats_mock")
def test_success_message_reports_actual_freed_bytes(display_mock_service: Mock) -> None:
    """The summary must report what was freed, not the pre-confirmation estimate."""
    display_mock_service.prompt_confirm.return_value = True

    assert cleanup_run([]) == 0
    out = _stdout(display_mock_service)
    assert "2 project log(s) deleted" in out
    assert "<2097152B>" in out
    assert "<3145728B>" not in display_mock_service.success.call_args[0][0]
    assert "1 stale registry entr(y/ies) removed" in out


# --- unfinalized archive ---------------------------------------------------


@pytest.mark.usefixtures("stats_mock")
def test_unfinalized_archive_reports_manual_fallback(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = True
    services.stats_service.run_cleanup.return_value = _outcome(  # type: ignore[union-attr]
        finalized=False, removed_paths=[]
    )

    assert cleanup_run([]) == 1
    message = display_mock_service.error.call_args[0][0]
    assert "failed to finalize" in message
    assert "mv /wrap/.agent-launches/orphaned-usage-archive.new.json" in message
    assert "/wrap/.agent-launches/orphaned-usage-archive.json" in message


# --- orphaned dirs / stale entries independently present -------------------


@pytest.mark.usefixtures("stats_mock")
def test_runs_with_only_stale_entries(display_mock_service: Mock) -> None:
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], freed_estimate=0)  # type: ignore[union-attr]
    display_mock_service.prompt_confirm.return_value = True

    assert cleanup_run([]) == 0
    out = _stdout(display_mock_service)
    assert "0 project log(s) will be deleted" in out
    assert "<0B>" in out


@pytest.mark.usefixtures("stats_mock")
def test_omits_stale_line_when_none(display_mock_service: Mock) -> None:
    services.stats_service.cleanup_scope.return_value = _scope(stale=[])  # type: ignore[union-attr]
    display_mock_service.prompt_confirm.return_value = True

    assert cleanup_run([]) == 0
    assert "stale project registry" not in _stdout(display_mock_service)


# --- spinners ---------------------------------------------------------------


@pytest.mark.usefixtures("stats_mock")
def test_scope_spinner_runs_before_empty_scope_check(display_mock_service: Mock) -> None:
    """The scan spinner must run even when there is nothing to clean up."""
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], stale=[])  # type: ignore[union-attr]

    assert cleanup_run([]) == 0
    display_mock_service.spin_while.assert_called_once()
    call = display_mock_service.spin_while.call_args
    assert call.kwargs["label"] == CLEANUP_LABEL
    assert call.kwargs["message"] == "scanning…"
    assert call.kwargs["done_message"]() is None
    assert callable(call.kwargs["work"])


@pytest.mark.usefixtures("stats_mock")
def test_scope_spinner_runs_on_dry_run(display_mock_service: Mock) -> None:
    assert cleanup_run(["--dry-run"]) == 0
    assert display_mock_service.spin_while.call_count == 1
    assert display_mock_service.spin_while.call_args[1]["label"] == CLEANUP_LABEL
    assert display_mock_service.spin_while.call_args[1]["message"] == "scanning…"


@pytest.mark.usefixtures("stats_mock")
def test_cleanup_spinner_not_run_on_decline(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = False

    assert cleanup_run([]) == 0
    assert display_mock_service.spin_while.call_count == 1


@pytest.mark.usefixtures("stats_mock")
def test_cleanup_spinner_runs_after_confirmation(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = True

    assert cleanup_run([]) == 0
    assert display_mock_service.spin_while.call_count == 2
    second_call = display_mock_service.spin_while.call_args_list[1]
    assert second_call.kwargs["label"] == CLEANUP_LABEL
    assert second_call.kwargs["message"] == "cleaning up…"
    services.stats_service.run_cleanup.assert_called_once()  # type: ignore[union-attr]


# --- completion ------------------------------------------------------------


def test_complete_bare_tab_shows_flag() -> None:
    assert "--dry-run" in cleanup_complete(2, ["agent", "cleanup", ""])


def test_complete_flag_consumed() -> None:
    assert "--dry-run" not in cleanup_complete(3, ["agent", "cleanup", "--dry-run", ""])
