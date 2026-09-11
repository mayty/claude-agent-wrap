# This file has been created with the assistance of an AI tool.
"""The `cleanup` subcommand — removes leftover state from deleted projects."""

import contextlib
from typing import TYPE_CHECKING

import click

from agent_wrap.cli.cleanup.constants import (
    CLEANUP_IMAGE_ALIGNS,
    CLEANUP_IMAGE_ELIDE,
    CLEANUP_IMAGE_HEADERS,
    CLEANUP_IMAGE_TITLE,
    CLEANUP_LABEL,
    INDEX_RECLAIM_SKIPPED,
    INDEX_SWEEP_NOTE,
    RETENTION_CONSEQUENCE,
    RETENTION_NOTE,
    SKIPPED_IMAGE_NOTE,
    STALE_REBUILD_NOTE,
    UNATTRIBUTABLE_NOTE,
)
from agent_wrap.constants import DIVIDER
from agent_wrap.containers import core, services
from agent_wrap.domain.build.constants import (
    IMAGE_CLEANUP_GROUP_TEXT,
    IMAGE_CLEANUP_REASON_TEXT,
    ImageCleanupReason,
)
from agent_wrap.domain.display.constants import Ansi
from agent_wrap.domain.display.models import RowItem

if TYPE_CHECKING:
    from agent_wrap.domain.build.models import ImageCleanupOutcome, ImageCleanupScope
    from agent_wrap.domain.display.models import RowItemOrDivider
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.logs.models import IndexReclaim, RetentionScope
    from agent_wrap.domain.stats.models import CleanupOutcome, CleanupScope


class _CleanupReport:
    """What `agent cleanup` prints, before the prompt and after the work."""

    @staticmethod
    def image_table(scope: ImageCleanupScope, dsp: DisplayService) -> list[str]:
        """
        Render the outdated images as one table, grouped by why each is going.

        Grouped rather than sorted because the four reasons cost the reader different
        things — a superseded build is pure reclaim, while a stale project image buys a
        rebuild — and a group heading is where that gets said once instead of per row.
        Groups keep the enum's order, so the rows that cost nothing read first, and a
        reason with no rows contributes neither heading nor divider.

        Sizes are docker's own per-image figures and are never totalled: images share
        layers, so a sum would overstate the reclaim badly. The title counts images,
        matching the list ``remove_images`` will be handed.
        """
        body: list[RowItemOrDivider] = []
        for reason in ImageCleanupReason:
            rows = [image for image in scope.images if image.reason is reason]
            if not rows:
                continue
            if body:
                body.append(DIVIDER)
            body.append(
                RowItem(
                    cells=[IMAGE_CLEANUP_GROUP_TEXT[reason].format(count=len(rows)), "", ""],
                    style=Ansi.DIM,
                    prefix_len=0,
                )
            )
            body.extend(
                RowItem(
                    cells=[
                        image.display,
                        image.size,
                        IMAGE_CLEANUP_REASON_TEXT[reason].format(detail=image.detail),
                    ],
                    style=Ansi.BOLD_YELLOW,
                    prefix_len=0,
                )
                for image in rows
            )

        headers = list(CLEANUP_IMAGE_HEADERS)
        # Every column is measured here (leading=0), unlike the inspect tables that hold a
        # path tree back as a per-table leading column: there is one table and no tree, so
        # `leading` plus the shared count has to add up to all three headers.
        shared = dsp.compute_shared_widths([(headers, body, 0)], len(headers))
        return dsp.render_table(
            CLEANUP_IMAGE_TITLE.format(count=len(scope.images)),
            headers,
            list(CLEANUP_IMAGE_ALIGNS),
            body,
            0,
            shared,
            elide=CLEANUP_IMAGE_ELIDE,
        )

    @staticmethod
    def preview(
        scope: CleanupScope,
        image_scope: ImageCleanupScope,
        retention: RetentionScope,
        dsp: DisplayService,
    ) -> None:
        """
        Describe everything the run would remove, in the order it would remove it.

        Each half is silent when it has nothing, so a cleanup that is only about images
        does not print a line claiming zero logs — and vice versa. Retention is silent
        twice over: it says nothing when it is switched off, which is the default, and
        nothing when it is on but nothing is old enough yet.
        """
        if not scope.is_empty:
            dsp.info(
                f"{len(scope.orphaned_dirs)} project log(s) will be deleted, "
                f"freeing ~{dsp.format_bytes(scope.freed_estimate)}."
            )
            dsp.info("Their spend will no longer appear in `agent stats` under <orphaned>.")
            if scope.stale_paths:
                dsp.info(
                    f"{len(scope.stale_paths)} stale project registry entr(y/ies) will be removed."
                )
        if not image_scope.is_empty:
            for line in _CleanupReport.image_table(image_scope, dsp):
                dsp.info(line)
            if any(image.reason is ImageCleanupReason.STALE for image in image_scope.images):
                dsp.info(STALE_REBUILD_NOTE)
        if image_scope.unattributable:
            dsp.info(UNATTRIBUTABLE_NOTE.format(count=image_scope.unattributable))
        if not retention.is_empty:
            dsp.info(
                RETENTION_NOTE.format(
                    count=len(retention.sessions),
                    days=retention.days,
                    size=dsp.format_bytes(retention.freed_estimate),
                )
            )
            dsp.info(RETENTION_CONSEQUENCE)
        # Unconditional, because it is the only thing that ever says it: the sweep runs
        # on every real cleanup, including one that has no orphaned logs at all. A
        # session re-ingested after its log file was replaced leaves content behind that
        # no request reaches, and this verb is the only place that reclaims it.
        dsp.info(INDEX_SWEEP_NOTE)

    @staticmethod
    def summarize(
        outcome: CleanupOutcome,
        image_outcome: ImageCleanupOutcome,
        reclaim: IndexReclaim | None,
        dsp: DisplayService,
    ) -> int:
        """
        Report what the run actually did, and return the command's exit code.

        A skipped image is a warning rather than a failure: ``remove_images`` never
        forces, so docker refusing one is the safety net working, and the rest of the run
        still happened. A reclaim that did not run is reported the same way and for the
        same reason: everything else still happened, and the content it would have
        reclaimed is still reachable-or-not exactly as it was.

        Retention gets its own line whenever it deleted anything, rather than being
        folded into the cleanup total. The two are different questions — one is about
        projects that are gone, the other about sessions in projects that are not — and
        a reader has to be able to see which of them took a directory.
        """
        for image in image_outcome.skipped:
            dsp.warning(f"{image.display}: {SKIPPED_IMAGE_NOTE}")

        result = outcome.result
        dsp.success(
            f"Cleanup complete: {result.removed} project log(s) deleted "
            f"({dsp.format_bytes(result.freed_bytes)} freed), "
            f"{len(outcome.removed_paths)} stale registry entr(y/ies) removed, "
            f"{len(image_outcome.removed)} image(s) removed."
        )
        if reclaim is None:
            dsp.warning(INDEX_RECLAIM_SKIPPED)
            return 0
        if reclaim.retention.sessions:
            dsp.info(
                f"Retention deleted {reclaim.retention.sessions} expired session log(s) "
                f"({dsp.format_bytes(reclaim.retention.freed_bytes)} freed)."
            )
        if reclaim.sweep.removed:
            dsp.info(
                f"Reclaimed {dsp.format_bytes(reclaim.sweep.freed_bytes)} from the request "
                f"index ({reclaim.sweep.removed} unreachable blob(s))."
            )
        return 0


@click.command("cleanup")
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Show what would be cleaned up, without deleting anything or prompting.",
)
@click.pass_context
def cleanup_command(ctx: click.Context, *, dry_run: bool) -> None:
    """
    Delete orphaned project data and outdated images

    Remove the leftover state that accumulates as projects are deleted or renamed and
    images are rebuilt: request-log directories no longer reachable from any registered
    project, registry entries whose project directory is gone, and docker images that are
    untagged, orphaned, stale, or superseded by a newer pinned sidecar digest.

    A deleted log directory's spend stops appearing in `agent stats`: the totals are
    aggregated from the request index, and the index forgets a project along with its
    logs. The whole plan is previewed and confirmed once before anything is removed.

    When AGENT_LOGS_RETENTION_DAYS is set, sessions with no activity for that many days
    are deleted too — their log files and their index rows together — and appear in the
    same preview. Retention is off unless that variable is set.
    """
    dsp = services.display_service
    stats = services.stats_service
    build = services.build_service
    logs = services.logs_service

    def survey() -> tuple[CleanupScope, ImageCleanupScope, RetentionScope]:
        """All three surveys, so one spinner covers the whole scan. None changes anything."""
        return (
            stats.cleanup_scope(),
            build.image_cleanup_scope(services.config_service.read_project_paths()),
            logs.retention_scope(),
        )

    # The survey's only write is the one-time projects.txt import, and it has to happen
    # here rather than before `clean`: the scope `clean` deletes from is built in this
    # spinner, and an un-imported registry makes every project's log dir look orphaned.
    # --dry-run takes no grant and so previews exactly that -- read_project_paths says
    # why on stderr.
    with contextlib.nullcontext() if dry_run else core.projects_db.enable_writes():
        scope, image_scope, retention_scope = dsp.spin_while(
            label=CLEANUP_LABEL, message="scanning…", work=survey
        )

    if scope.is_empty and image_scope.is_empty and retention_scope.is_empty:
        dsp.info(
            "Nothing to clean up: no orphaned logs, stale registry entries or outdated "
            "images found."
        )
        if image_scope.unattributable:
            dsp.info(UNATTRIBUTABLE_NOTE.format(count=image_scope.unattributable))
        ctx.exit(0)

    _CleanupReport.preview(scope, image_scope, retention_scope, dsp)

    if dry_run:
        ctx.exit(0)

    # Non-interactive stdin declines via prompt_confirm's EOFError handling.
    if not dsp.prompt_confirm("Proceed? [y/N]"):
        dsp.info("Cleanup cancelled.")
        ctx.exit(0)

    def clean() -> tuple[ImageCleanupOutcome, CleanupOutcome, IndexReclaim | None]:
        """
        Remove the images first, then the logs, their index rows, and the registry.

        Images first because their removal is independent of everything else: docker
        refusing one is reported as a warning and changes nothing about the log and
        registry work that follows.

        The reclaim goes last, and has to. It applies retention and then sweeps, and
        the sweep deletes the content no request reaches any more — so it must run
        after every row that referenced it is gone, whether the orphan delete above or
        retention inside it took that row. Reversed, it would find every one of those
        blobs still reachable and reclaim nothing.
        """
        # Bound to names rather than returned as one tuple literal -- unlike `survey`, the
        # order here is the contract the docstring describes, and a literal evaluates in
        # the same order while reading as though it did not matter.
        image_outcome = build.remove_images(image_scope)
        outcome = stats.run_cleanup(scope)
        reclaim = logs.reclaim_index(retention_scope)
        return image_outcome, outcome, reclaim

    # Two grants, constructed fresh rather than reusing the registry one above: a
    # @contextmanager instance is single-use, and nullcontext is not, so a shared
    # variable would work under --dry-run and break on every real run. Unconditional
    # because the --dry-run exit is above. The registry grant is what lets `clean` prune
    # stale entries -- without it prune_stale_projects suppresses the refusal, returns
    # the paths anyway, and the summary reports entries as removed that are still there.
    # The logs grant is what lets it forget a deleted project's requests; without it the
    # directories would go while their spend stayed in `agent stats` forever.
    with core.projects_db.enable_writes(), core.logs_db.enable_writes():
        image_outcome, outcome, reclaim = dsp.spin_while(
            label=CLEANUP_LABEL, message="cleaning up…", work=clean
        )
    ctx.exit(_CleanupReport.summarize(outcome, image_outcome, reclaim, dsp))
