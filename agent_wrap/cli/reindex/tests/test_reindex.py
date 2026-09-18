# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.reindex — grants, reporting, and exit codes."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.containers import services
from agent_wrap.domain.logs.models import (
    IndexReclaim,
    IngestReport,
    RetentionResult,
    RetentionScope,
)
from agent_wrap.infrastructure.logs.models import BlobSweep

if TYPE_CHECKING:
    from unittest.mock import Mock

    from click.testing import CliRunner


def _report(
    *,
    seen: int = 3,
    changed: int = 2,
    reset: int = 0,
    records: int = 41,
    failed: tuple[tuple[Path, str], ...] = (),
) -> IngestReport:
    return IngestReport(
        sessions_seen=seen,
        sessions_changed=changed,
        sessions_reset=reset,
        records_ingested=records,
        failed=failed,
    )


@pytest.fixture
def logs_mock() -> Mock:
    """Return the mocked LogsService, pre-seeded with a successful ingest pass."""
    logs = services.logs_service
    logs.ingest_tree.return_value = _report()  # pyrefly: ignore [missing-attribute]
    return logs


@pytest.fixture
def dsp_mock() -> Mock:
    """
    Return the mocked DisplayService with the two behaviours this verb depends on.

    ``spin_while`` is a no-op mock by default, which would swallow the ingest call
    entirely, and ``format_count`` would return a ``Mock`` in the middle of the summary
    line. Both are given real behaviour so the assertions below read the text a user
    would actually see.
    """
    dsp = services.display_service
    dsp.spin_while.side_effect = lambda **kw: kw["work"]()  # pyrefly: ignore [missing-attribute]
    dsp.format_count.side_effect = str  # pyrefly: ignore [missing-attribute]
    return dsp


def _stdout(dsp: Mock) -> str:
    """Join everything the command printed, in no particular order."""
    return "\n".join(
        str(call.args[0])
        for call in (
            *dsp.info.call_args_list,
            *dsp.success.call_args_list,
            *dsp.warning.call_args_list,
            *dsp.error.call_args_list,
        )
    )


def test_a_successful_pass_reports_both_counts_and_exits_zero(
    runner: CliRunner, logs_mock: Mock, dsp_mock: Mock
) -> None:
    """
    Both figures, so "already up to date" is distinguishable from "found nothing".

    A run that changed nothing and a host with no logs are very different situations,
    and the summary line is the only place a user sees which one they are in.
    """
    result = runner.invoke(cli_root, ["reindex"])

    assert result.exit_code == 0
    assert "indexed 41 request(s) from 2 of 3 session(s)." in _stdout(dsp_mock)
    logs_mock.ingest_tree.assert_called_once_with()


def test_an_up_to_date_tree_still_reports_the_sessions_it_looked_at(
    runner: CliRunner, logs_mock: Mock, dsp_mock: Mock
) -> None:
    logs_mock.ingest_tree.return_value = _report(seen=613, changed=0, records=0)

    result = runner.invoke(cli_root, ["reindex"])

    assert result.exit_code == 0
    assert "indexed 0 request(s) from 0 of 613 session(s)." in _stdout(dsp_mock)


def test_an_empty_tree_says_so_instead_of_reporting_zeroes(
    runner: CliRunner, logs_mock: Mock, dsp_mock: Mock
) -> None:
    logs_mock.ingest_tree.return_value = _report(seen=0, changed=0, records=0)

    result = runner.invoke(cli_root, ["reindex"])

    assert result.exit_code == 0
    out = _stdout(dsp_mock)
    assert "nothing to index" in out
    assert "indexed" not in out


def test_rewritten_sessions_are_reported_separately(
    runner: CliRunner, logs_mock: Mock, dsp_mock: Mock
) -> None:
    """
    A re-read from zero is worth naming: it means a log file was replaced, not appended.

    Folding it into ``sessions_changed`` would hide the only signal that something wrote
    the tree in a way the append-only assumption does not cover.
    """
    logs_mock.ingest_tree.return_value = _report(reset=2)

    result = runner.invoke(cli_root, ["reindex"])

    assert result.exit_code == 0
    assert "2 session(s) had been rewritten" in _stdout(dsp_mock)


@pytest.mark.usefixtures("logs_mock")
def test_no_rewritten_sessions_prints_no_such_line(runner: CliRunner, dsp_mock: Mock) -> None:
    runner.invoke(cli_root, ["reindex"])

    assert "rewritten" not in _stdout(dsp_mock)


def test_a_failed_session_is_named_and_exits_one(
    runner: CliRunner, logs_mock: Mock, dsp_mock: Mock
) -> None:
    """
    Named individually rather than counted: a failure is a directory to go and look at.

    The non-zero exit is what makes it visible to a script, since the pass deliberately
    continues past the failure rather than aborting.
    """
    logs_mock.ingest_tree.return_value = _report(
        failed=((Path("/wrap/litellm-logs/hashA/bedrock/sess-1"), "write failed: locked"),)
    )

    result = runner.invoke(cli_root, ["reindex"])

    out = _stdout(dsp_mock)
    assert result.exit_code == 1
    assert "failed to index /wrap/litellm-logs/hashA/bedrock/sess-1: write failed: locked" in out
    # The sessions that did succeed are still reported -- the pass is not abandoned.
    assert "indexed 41 request(s)" in out


def test_every_failed_session_is_named(runner: CliRunner, logs_mock: Mock, dsp_mock: Mock) -> None:
    logs_mock.ingest_tree.return_value = _report(
        failed=((Path("/logs/a"), "one"), (Path("/logs/b"), "two"))
    )

    result = runner.invoke(cli_root, ["reindex"])

    out = _stdout(dsp_mock)
    assert result.exit_code == 1
    assert "failed to index /logs/a: one" in out
    assert "failed to index /logs/b: two" in out


def test_contention_does_nothing_and_exits_one(
    runner: CliRunner, logs_mock: Mock, dsp_mock: Mock
) -> None:
    """
    ``None`` means another process holds the ingest lock.

    Exiting non-zero rather than 0 is deliberate: the user asked for the tree to be
    indexed and it was not, even though the reason is benign, and a script driving this
    needs to know to come back.
    """
    logs_mock.ingest_tree.return_value = None

    result = runner.invoke(cli_root, ["reindex"])

    out = _stdout(dsp_mock)
    assert result.exit_code == 1
    assert "another process is already reading the log tree" in out
    assert "indexed" not in out


@pytest.mark.usefixtures("logs_mock", "dsp_mock")
def test_the_logs_database_grant_is_taken(runner: CliRunner, write_grants: list[str]) -> None:
    """Writing the logs database is this verb's whole purpose, so it must hold a grant."""
    runner.invoke(cli_root, ["reindex"])

    assert write_grants == ["logs"]


@pytest.mark.usefixtures("logs_mock", "dsp_mock")
def test_the_registry_is_never_granted(runner: CliRunner, write_grants: list[str]) -> None:
    """
    Reindex reads the log tree and writes the index; it has no business in the registry.

    Asserted separately from the positive case because a stray grant on the wrong
    database is exactly the kind of bug the fixture exists to catch, and a test that
    only checked "some grant was taken" would pass with the wrong one.
    """
    runner.invoke(cli_root, ["reindex"])

    assert "projects" not in write_grants


@pytest.mark.usefixtures("dsp_mock")
def test_contention_still_takes_the_grant_it_declared(
    runner: CliRunner, logs_mock: Mock, write_grants: list[str]
) -> None:
    """
    The grant wraps the attempt, not the outcome.

    Narrowing it to "only when the lock is won" would mean deciding whether to write
    before knowing whether there is anything to write, and the grant is a statement
    about the invocation rather than about what it turned out to do.
    """
    logs_mock.ingest_tree.return_value = None

    runner.invoke(cli_root, ["reindex"])

    assert write_grants == ["logs"]


def _marked_bytes(count: int) -> str:
    """Render a byte count as a marker no other line in the summary can produce."""
    return f"<{count}B>"


@pytest.fixture
def pruning(logs_mock: Mock, dsp_mock: Mock) -> Mock:
    """Seed the mocks as a host with retention on at 90 days and something to prune."""
    logs_mock.retention_days.return_value = 90
    logs_mock.retention_scope.return_value = RetentionScope(days=90, sessions=())
    logs_mock.reclaim_index.return_value = IndexReclaim(
        retention=RetentionResult(sessions=4, freed_bytes=8_388_608),
        sweep=BlobSweep(removed=91, freed_bytes=2_097_152),
    )
    dsp_mock.format_bytes.side_effect = _marked_bytes
    return logs_mock


@pytest.mark.usefixtures("dsp_mock")
def test_prune_without_the_variable_is_a_usage_error(runner: CliRunner, logs_mock: Mock) -> None:
    """
    A flag that cannot do anything is a mistake in the invocation, not a quiet no-op.

    Refused before the walk, too: the alternative is making the user watch a whole
    backfill finish before being told the flag was never going to prune.
    """
    logs_mock.retention_days.return_value = 0

    result = runner.invoke(cli_root, ["reindex", "--prune"])

    assert result.exit_code == 2
    assert "AGENT_LOGS_RETENTION_DAYS" in result.output
    logs_mock.ingest_tree.assert_not_called()
    logs_mock.reclaim_index.assert_not_called()


def test_prune_runs_after_the_index_is_up_to_date(
    runner: CliRunner, pruning: Mock, dsp_mock: Mock
) -> None:
    """
    Order is load-bearing: retention dates a session by what the index holds.

    Pruning first would judge an age from a watermark the pass immediately after it was
    about to move, so a session appended to this morning could read as months old.
    """
    order: list[str] = []

    def ingest() -> IngestReport:
        order.append("ingest")
        return _report()

    def reclaim(_scope: RetentionScope) -> IndexReclaim:
        order.append("prune")
        return IndexReclaim(
            retention=RetentionResult(sessions=4, freed_bytes=8_388_608),
            sweep=BlobSweep(removed=91, freed_bytes=2_097_152),
        )

    pruning.ingest_tree.side_effect = ingest
    pruning.reclaim_index.side_effect = reclaim

    result = runner.invoke(cli_root, ["reindex", "--prune"])

    assert result.exit_code == 0
    assert order == ["ingest", "prune"]
    assert "pruned 4 session(s) with no activity for 90 day(s)" in _stdout(dsp_mock)


def test_prune_reports_both_disks_it_reclaimed(
    runner: CliRunner, dsp_mock: Mock, pruning: Mock
) -> None:
    """
    Log-tree bytes and index bytes are separate figures because they are separate disks.

    Retention frees the first by deleting files; only the sweep that follows it frees
    the second, and a single total would hide that the second happened at all.
    """
    assert runner.invoke(cli_root, ["reindex", "--prune"]).exit_code == 0

    out = _stdout(dsp_mock)
    assert "<8388608B> of logs" in out
    assert "<2097152B> of indexed content" in out
    pruning.reclaim_index.assert_called_once_with(RetentionScope(days=90, sessions=()))


def test_prune_losing_the_lock_is_a_warning_not_a_failure(
    runner: CliRunner, dsp_mock: Mock, pruning: Mock
) -> None:
    """The index was still brought up to date, which is the part that was asked for."""
    pruning.reclaim_index.return_value = None

    result = runner.invoke(cli_root, ["reindex", "--prune"])

    assert result.exit_code == 0
    assert "another process took the ingest lock" in _stdout(dsp_mock)


@pytest.mark.usefixtures("pruning")
def test_no_prune_flag_never_reclaims_anything(runner: CliRunner, logs_mock: Mock) -> None:
    """Retention being configured is not on its own permission to apply it here."""
    runner.invoke(cli_root, ["reindex"])

    logs_mock.reclaim_index.assert_not_called()


@pytest.mark.usefixtures("pruning")
def test_prune_takes_the_logs_grant_for_its_own_writes(
    runner: CliRunner, write_grants: list[str]
) -> None:
    """
    Two grants, one per writing phase: the ingest pass and then the reclaim.

    A ``@contextmanager`` instance is single-use, so the second cannot reuse the first
    -- and deleting rows outside a grant would be suppressed rather than raised.
    """
    runner.invoke(cli_root, ["reindex", "--prune"])

    assert write_grants == ["logs", "logs"]
