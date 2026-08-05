# This file has been created with the assistance of an AI tool.
"""The `cleanup` subcommand — removes leftover state from deleted projects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.cli.cleanup.constants import CLEANUP_LABEL
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

    from agent_wrap.domain.stats.models import CleanupOutcome, CleanupScope

USAGE = "[-n|--dry-run]"
SUMMARY = "Delete orphaned project data (archiving usage first)"


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("cleanup", usage_summary=USAGE)
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be cleaned up, without deleting anything or prompting.",
    )
    return parser


def run(args: list[str]) -> int:
    """Execute the `cleanup` subcommand."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns
    dsp = services.display_service
    stats = services.stats_service

    scoped: list[CleanupScope] = []
    dsp.spin_while(
        label=CLEANUP_LABEL,
        message="scanning…",
        done_message=lambda: None,
        work=lambda: scoped.append(stats.cleanup_scope()),
    )
    scope = scoped[0]

    if scope.is_empty:
        dsp.info("Nothing to clean up: no orphaned logs or stale registry entries found.")
        return 0

    dsp.info(
        f"{len(scope.orphaned_dirs)} project log(s) will be deleted, "
        f"freeing ~{dsp.format_bytes(scope.freed_estimate)}."
    )
    if scope.stale_paths:
        dsp.info(f"{len(scope.stale_paths)} stale project registry entr(y/ies) will be removed.")

    if ns.dry_run:
        return 0

    # Non-interactive stdin declines via prompt_confirm's EOFError handling.
    if not dsp.prompt_confirm("Proceed? [y/N]"):
        dsp.info("Cleanup cancelled.")
        return 0

    outcomes: list[CleanupOutcome] = []
    dsp.spin_while(
        label=CLEANUP_LABEL,
        message="cleaning up…",
        done_message=lambda: None,
        work=lambda: outcomes.append(stats.run_cleanup(scope)),
    )
    result = outcomes[0].result
    if not result.finalized:
        dsp.error(
            "Deleted logs but failed to finalize the usage archive. "
            f"Run: mv {result.staging_path} {result.archive_path}"
        )
        return 1

    dsp.success(
        f"Cleanup complete: {result.removed} project log(s) deleted "
        f"({dsp.format_bytes(result.freed_bytes)} freed), "
        f"{len(outcomes[0].removed_paths)} stale registry entr(y/ies) removed."
    )
    return 0
