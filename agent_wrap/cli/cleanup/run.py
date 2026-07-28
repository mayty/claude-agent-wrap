# This file has been created with the assistance of an AI tool.
"""The `cleanup` subcommand — removes leftover state from deleted projects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.cli.cleanup.constants import CLEANUP_LABEL
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

    from agent_wrap.domain.stats.models import CleanupResult

USAGE = "[--dry-run]"
SUMMARY = "Delete orphaned project data (archiving usage first)"


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("cleanup", usage_summary=USAGE)
    parser.add_argument(
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
    config = services.config_service
    stats = services.stats_service

    captured_scope: list[tuple[list[Path], list[Path], int]] = []

    def _compute_scope() -> None:
        orphaned_dirs = stats.orphaned_log_dirs(config.read_project_paths())
        stale_paths = config.stale_project_paths()
        # Measured before anything is deleted, and reused for --dry-run's
        # preview — it describes exactly the dirs the confirmed run will remove.
        freed_estimate = stats.orphaned_disk_usage(orphaned_dirs)
        captured_scope.append((orphaned_dirs, stale_paths, freed_estimate))

    dsp.spin_while(
        label=CLEANUP_LABEL,
        message="scanning…",
        done_message=lambda: None,
        work=_compute_scope,
    )
    orphaned_dirs, stale_paths, freed_estimate = captured_scope[0]

    if not orphaned_dirs and not stale_paths:
        dsp.info("Nothing to clean up: no orphaned logs or stale registry entries found.")
        return 0

    dsp.info(
        f"{len(orphaned_dirs)} project log(s) will be deleted, "
        f"freeing ~{dsp.format_bytes(freed_estimate)}."
    )
    if stale_paths:
        dsp.info(f"{len(stale_paths)} stale project registry entr(y/ies) will be removed.")

    if ns.dry_run:
        return 0

    # Non-interactive stdin declines via prompt_confirm's EOFError handling.
    if not dsp.prompt_confirm("Proceed? [y/N]"):
        dsp.info("Cleanup cancelled.")
        return 0

    captured_result: list[CleanupResult] = []
    dsp.spin_while(
        label=CLEANUP_LABEL,
        message="cleaning up…",
        done_message=lambda: None,
        work=lambda: captured_result.append(stats.archive_and_delete_orphaned(orphaned_dirs)),
    )
    result = captured_result[0]
    if not result.finalized:
        # The logs are gone but their usage only reached the staging file. Leave
        # the registry alone so the surviving evidence stays consistent.
        dsp.error(
            "Deleted logs but failed to finalize the usage archive. "
            f"Run: mv {result.staging_path} {result.archive_path}"
        )
        return 1

    removed_paths = config.prune_stale_projects(stale_paths)
    dsp.success(
        f"Cleanup complete: {result.removed} project log(s) deleted "
        f"({dsp.format_bytes(result.freed_bytes)} freed), "
        f"{len(removed_paths)} stale registry entr(y/ies) removed."
    )
    return 0
