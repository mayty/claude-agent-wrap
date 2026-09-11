# This file has been created with the assistance of an AI tool.
"""The `reindex` subcommand — bring the logs database up to date with the log tree."""

from typing import TYPE_CHECKING

import click

from agent_wrap.containers import core, services
from agent_wrap.domain.logs.constants import REINDEX_LABEL

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


@click.command("reindex")
@click.option(
    "--prune",
    is_flag=True,
    help="After indexing, delete sessions older than AGENT_LOGS_RETENTION_DAYS.",
)
@click.pass_context
def reindex_command(ctx: click.Context, *, prune: bool) -> None:
    """
    Re-read the LiteLLM log tree into the logs database

    Walk every session directory under <wrap-dir>/litellm-logs and ingest whatever each
    one has gained since it was last read. `agent stats` and the `agent logs` viewer
    read only the database, so this is what you run when a host has logs that predate
    the database, or when the viewer has not been running to ingest them as they
    arrived.

    Incremental, and safe to repeat. Each session records a byte offset into its own log
    files, so a second run reads nothing and reports zero sessions changed rather than
    duplicating anything. A session whose log file is shorter than its recorded offset
    was replaced rather than appended to, and is dropped and re-read from the start.

    Ingest is serialized host-wide: if the background viewer or another reindex is
    already reading the tree, this exits without doing anything rather than competing
    with it.

    `--prune` additionally applies age-based retention, which deletes a session's log
    files along with its index rows and is therefore permanent. It does nothing on its
    own: the age comes from AGENT_LOGS_RETENTION_DAYS, and without that the flag is
    refused rather than treated as a no-op.
    """
    dsp = services.display_service
    logs = services.logs_service
    reports = []

    # Checked before the walk, not after it: a `--prune` that cannot prune is a mistake
    # in the invocation, and saying so costs nothing next to a backfill the user would
    # then have to watch finish first.
    if prune and not logs.retention_days():
        message = (
            "--prune needs AGENT_LOGS_RETENTION_DAYS set to a number of days. Retention "
            "is off by default: with no age to compare against, there is nothing to prune."
        )
        raise click.UsageError(message)

    # The database is this verb's whole purpose, so unlike `agent stats` -- which reports
    # from it and is deliberately ungranted -- the grant is unconditional here. It covers
    # the ingest call only; the walk and the report do not write.
    with core.logs_db.enable_writes():
        dsp.spin_while(
            label=REINDEX_LABEL,
            message="reading log tree…",
            done_message=lambda: None,
            work=lambda: reports.append(logs.ingest_tree()),
        )

    report = reports[0]
    if report is None:
        dsp.warning(
            "another process is already reading the log tree (the `agent logs` viewer, "
            "or a concurrent `agent reindex`). Nothing was done — try again in a moment."
        )
        ctx.exit(1)

    if not report.sessions_seen:
        dsp.info("no LiteLLM log sessions found — nothing to index.")
    else:
        dsp.success(
            f"indexed {dsp.format_count(report.records_ingested)} request(s) from "
            f"{report.sessions_changed} of {report.sessions_seen} session(s)."
        )
        if report.sessions_reset:
            dsp.info(
                f"{report.sessions_reset} session(s) had been rewritten rather than appended "
                "to, and were re-read from the start."
            )

    # Named individually rather than counted: a failing session is a specific directory
    # someone has to look at, and there is no aggregate that helps them find it.
    for session_dir, message in report.failed:
        dsp.error(f"failed to index {session_dir}: {message}")

    if prune:
        _prune(dsp)
    ctx.exit(0 if report.ok else 1)


def _prune(dsp: DisplayService) -> None:
    """
    Apply retention and sweep, after the index has been brought up to date.

    Ingest first and prune second, deliberately. Retention dates a session by the newest
    request the *index* holds, so pruning first would judge an age from a watermark that
    the pass immediately after it was about to move — a session appended to this morning
    would read as months old right up until the moment it stopped being true.

    No prompt. Unlike `agent cleanup`, which surveys a set the user did not ask for and
    has to show it first, this is a flag plus an environment variable: both halves were
    stated by whoever ran the command.
    """
    scope = services.logs_service.retention_scope()
    with core.logs_db.enable_writes():
        reclaim = services.logs_service.reclaim_index(scope)
    if reclaim is None:
        dsp.warning(
            "another process took the ingest lock before the prune could run. Nothing "
            "was deleted — re-run `agent reindex --prune`."
        )
        return
    dsp.success(
        f"pruned {reclaim.retention.sessions} session(s) with no activity for "
        f"{scope.days} day(s), freeing {dsp.format_bytes(reclaim.retention.freed_bytes)} "
        f"of logs and {dsp.format_bytes(reclaim.sweep.freed_bytes)} of indexed content."
    )
