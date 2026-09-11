# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.cleanup — parsing, prompting, and reporting."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli.cleanup.constants import CLEANUP_LABEL
from agent_wrap.containers import services
from agent_wrap.domain.build.models import ImageCleanupOutcome, ImageCleanupScope
from agent_wrap.domain.logs.models import (
    ExpiredSession,
    IndexReclaim,
    RetentionResult,
    RetentionScope,
)
from agent_wrap.domain.stats.models import CleanupOutcome, CleanupResult, CleanupScope
from agent_wrap.infrastructure.logs.models import BlobSweep, SessionKey

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


def _outcome(*, removed_paths: list[Path] | None = None) -> CleanupOutcome:
    return CleanupOutcome(
        result=CleanupResult(removed=2, freed_bytes=2_097_152),
        removed_paths=list(_STALE) if removed_paths is None else removed_paths,
    )


@pytest.fixture
def stats_mock() -> Mock:
    """Return the mocked StatsService, pre-seeded with a scope and a successful run."""
    stats = services.stats_service
    stats.cleanup_scope.return_value = _scope()  # pyrefly: ignore [missing-attribute]
    stats.run_cleanup.return_value = _outcome()  # pyrefly: ignore [missing-attribute]
    return stats


def _reclaim(
    *, sessions: int = 0, retention_bytes: int = 0, removed: int = 0, freed_bytes: int = 0
) -> IndexReclaim:
    return IndexReclaim(
        retention=RetentionResult(sessions=sessions, freed_bytes=retention_bytes),
        sweep=BlobSweep(removed=removed, freed_bytes=freed_bytes),
    )


@pytest.fixture(autouse=True)
def logs_mock() -> Mock:
    """
    Return the mocked LogsService, seeded as a host that reclaimed nothing.

    Autouse and silent by default, so the cases in this file that predate the blob
    sweep and retention read as they did: retention switched off adds no line to the
    preview, and a sweep that found nothing adds none to the summary.
    """
    logs = services.logs_service
    logs.retention_scope.return_value = RetentionScope(  # pyrefly: ignore [missing-attribute]
        days=0, sessions=()
    )
    logs.reclaim_index.return_value = _reclaim()  # pyrefly: ignore [missing-attribute]
    return logs


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
def test_the_preview_says_the_spend_will_stop_being_reported(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """
    Deleting logs now deletes their spend, and the prompt has to say so before it asks.

    It used to be archived and kept appearing under ``<orphaned>`` forever. Removing
    that is a change in what a user loses by confirming, so the preview names it rather
    than leaving them to notice afterwards.
    """
    display_mock_service.prompt_confirm.return_value = False

    runner.invoke(cli_root, ["cleanup"])

    out = _stdout(display_mock_service)
    assert "no longer appear in `agent stats` under <orphaned>" in out


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


@pytest.mark.usefixtures("stats_mock")
def test_cleanup_takes_a_registry_write_grant_per_writing_phase(
    runner: CliRunner, display_mock_service: Mock, write_grants: list[str]
) -> None:
    """
    Three grants: one per phase that writes, and one per database the clean writes.

    The survey takes a registry grant so the one-time ``projects.txt`` import lands
    before the scope is computed. The clean takes a registry grant to prune stale
    entries and a logs grant to forget a deleted project's requests. The phases between
    them — preview, confirm — touch no database and hold nothing.
    """
    display_mock_service.prompt_confirm.return_value = True

    runner.invoke(cli_root, ["cleanup"])

    assert write_grants == ["projects", "projects", "logs"]


@pytest.mark.usefixtures("stats_mock", "display_mock_service")
def test_cleanup_dry_run_takes_no_registry_write_grant(
    runner: CliRunner, write_grants: list[str]
) -> None:
    """
    A preview must not be able to mutate anything, the legacy import included.

    The grant covers the survey rather than just the deletion, because the scope the
    deletion acts on is built there -- so ``--dry-run`` has to withhold it from the whole
    command, not merely from the part that deletes.
    """
    runner.invoke(cli_root, ["cleanup", "--dry-run"])

    assert write_grants == []


@pytest.mark.usefixtures("stats_mock")
def test_the_preview_says_unreachable_index_content_will_go(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """
    The sweep is not part of the surveyed scope, so the preview is the only warning.

    Counting what it would reclaim costs the same full pass as reclaiming it, so the
    preview states that it happens rather than how much it will find.
    """
    display_mock_service.prompt_confirm.return_value = False

    runner.invoke(cli_root, ["cleanup"])

    assert "no request reaches any more will be reclaimed" in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock")
def test_the_sweep_runs_after_the_logs_are_forgotten(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """
    Order is the whole correctness of it: the rows first, then the content they held.

    Reversed, every blob would still be reachable and the sweep would reclaim nothing.
    """
    display_mock_service.prompt_confirm.return_value = True
    order: list[str] = []

    def delete(_scope: CleanupScope) -> CleanupOutcome:
        order.append("delete")
        return _outcome()

    def reclaim(_scope: RetentionScope) -> IndexReclaim:
        order.append("reclaim")
        return _reclaim()

    services.stats_service.run_cleanup.side_effect = delete  # pyrefly: ignore [missing-attribute]
    services.logs_service.reclaim_index.side_effect = reclaim  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert order == ["delete", "reclaim"]


@pytest.mark.usefixtures("stats_mock")
def test_a_sweep_that_reclaimed_something_reports_it(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    display_mock_service.prompt_confirm.return_value = True
    services.logs_service.reclaim_index.return_value = _reclaim(  # pyrefly: ignore [missing-attribute]
        removed=17, freed_bytes=4_194_304
    )

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = _stdout(display_mock_service)
    assert "Reclaimed <4194304B> from the request index (17 unreachable blob(s))." in out


@pytest.mark.usefixtures("stats_mock")
def test_a_sweep_the_ingest_lock_blocked_is_a_warning_not_a_failure(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """Everything else in the run happened, and nothing was lost by skipping it."""
    display_mock_service.prompt_confirm.return_value = True
    services.logs_service.reclaim_index.return_value = None  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    display_mock_service.success.assert_called_once()
    assert "another process is ingesting" in display_mock_service.warning.call_args[0][0]


def _expired(count: int = 2, *, size: int = 1_048_576) -> RetentionScope:
    """Build a retention scope of *count* expired sessions, each *size* bytes."""
    return RetentionScope(
        days=90,
        sessions=tuple(
            ExpiredSession(
                key=SessionKey(
                    project_hash="hashA", provider="litellm-bedrock", claude_session_id=f"s{i}"
                ),
                path=Path(f"/wrap/litellm-logs/hashA/litellm-bedrock/s{i}"),
                messages_offset=4096,
                size_bytes=size,
            )
            for i in range(count)
        ),
    )


@pytest.mark.usefixtures("stats_mock")
def test_the_preview_names_what_retention_would_delete(
    runner: CliRunner, display_mock_service: Mock, logs_mock: Mock
) -> None:
    """
    Surveyed, unlike the sweep: this one deletes log files, so it is priced before the prompt.

    The consequence line goes with it. Losing the spend permanently is the whole reason
    the variable is off by default, so the run that acts on it says so.
    """
    logs_mock.retention_scope.return_value = _expired()
    display_mock_service.prompt_confirm.return_value = False

    runner.invoke(cli_root, ["cleanup"])

    out = _stdout(display_mock_service)
    assert "2 indexed session log(s) have had no activity for 90 day(s)" in out
    assert "<2097152B>" in out
    assert "leaves 'agent stats' permanently" in out


@pytest.mark.usefixtures("stats_mock")
def test_retention_switched_off_says_nothing_at_all(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """The default. A line on every cleanup announcing a disabled feature is noise."""
    display_mock_service.prompt_confirm.return_value = False

    runner.invoke(cli_root, ["cleanup"])

    assert "no activity for" not in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock")
def test_expired_sessions_alone_are_enough_to_run(
    runner: CliRunner, display_mock_service: Mock, logs_mock: Mock
) -> None:
    """
    A host with nothing orphaned and no stale images can still have sessions to prune.

    Without retention in the emptiness test the command would print "nothing to clean
    up" and exit while holding a scope that names directories it was about to delete.
    """
    services.stats_service.cleanup_scope.return_value = _scope(orphaned=[], stale=[])  # pyrefly: ignore [missing-attribute]
    logs_mock.retention_scope.return_value = _expired(1)
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "Nothing to clean up" not in _stdout(display_mock_service)
    logs_mock.reclaim_index.assert_called_once_with(_expired(1))


@pytest.mark.usefixtures("stats_mock")
def test_retention_gets_its_own_summary_line(
    runner: CliRunner, display_mock_service: Mock, logs_mock: Mock
) -> None:
    """
    Not folded into the cleanup total: a reader has to see which half took a directory.

    One is about projects that are gone, the other about sessions inside projects that
    are not, and the two answers lead to different places.
    """
    logs_mock.retention_scope.return_value = _expired()
    logs_mock.reclaim_index.return_value = _reclaim(sessions=2, retention_bytes=2_097_152)
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = _stdout(display_mock_service)
    assert "Retention deleted 2 expired session log(s) (<2097152B> freed)." in out


@pytest.mark.usefixtures("stats_mock")
def test_retention_that_deleted_nothing_prints_no_line(
    runner: CliRunner, display_mock_service: Mock, logs_mock: Mock
) -> None:
    """A survey can name a session the run then refuses -- a file that grew in between."""
    logs_mock.retention_scope.return_value = _expired()
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "Retention deleted" not in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock")
def test_dry_run_previews_retention_without_applying_it(
    runner: CliRunner, display_mock_service: Mock, logs_mock: Mock
) -> None:
    logs_mock.retention_scope.return_value = _expired()

    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    assert "no activity for 90 day(s)" in _stdout(display_mock_service)
    logs_mock.reclaim_index.assert_not_called()
