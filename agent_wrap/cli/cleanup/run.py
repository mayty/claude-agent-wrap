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
    def preview(scope: CleanupScope, image_scope: ImageCleanupScope, dsp: DisplayService) -> None:
        """
        Describe everything the run would remove, in the order it would remove it.

        Each half is silent when it has nothing, so a cleanup that is only about images
        does not print a line claiming zero logs — and vice versa.
        """
        if not scope.is_empty:
            dsp.info(
                f"{len(scope.orphaned_dirs)} project log(s) will be deleted, "
                f"freeing ~{dsp.format_bytes(scope.freed_estimate)}."
            )
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

    @staticmethod
    def summarize(
        outcome: CleanupOutcome, image_outcome: ImageCleanupOutcome, dsp: DisplayService
    ) -> int:
        """
        Report what the run actually did, and return the command's exit code.

        A skipped image is a warning rather than a failure: ``remove_images`` never
        forces, so docker refusing one is the safety net working, and the rest of the run
        still happened. An unfinalized usage archive *is* a failure, and the one the
        caller has to act on by hand — it is reported last so the manual ``mv`` is the
        final line on screen.
        """
        for image in image_outcome.skipped:
            dsp.warning(f"{image.display}: {SKIPPED_IMAGE_NOTE}")

        result = outcome.result
        if not result.finalized:
            dsp.error(
                "Deleted logs but failed to finalize the usage archive. "
                f"Run: mv {result.staging_path} {result.archive_path}"
            )
            return 1

        dsp.success(
            f"Cleanup complete: {result.removed} project log(s) deleted "
            f"({dsp.format_bytes(result.freed_bytes)} freed), "
            f"{len(outcome.removed_paths)} stale registry entr(y/ies) removed, "
            f"{len(image_outcome.removed)} image(s) removed."
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

    Every log directory's token counts are archived before it is deleted, so historical
    spend keeps appearing in `agent stats` under the <orphaned> row. The whole plan is
    previewed and confirmed once before anything is removed.
    """
    dsp = services.display_service
    stats = services.stats_service
    build = services.build_service

    scoped: list[CleanupScope] = []
    image_scoped: list[ImageCleanupScope] = []

    def survey() -> None:
        """Both surveys, so one spinner covers the whole scan. Neither changes anything."""
        scoped.append(stats.cleanup_scope())
        image_scoped.append(build.image_cleanup_scope(services.config_service.read_project_paths()))

    # The survey's only write is the one-time projects.txt import, and it has to happen
    # here rather than before `clean`: the scope `clean` deletes from is built in this
    # spinner, and an un-imported registry makes every project's log dir look orphaned.
    # --dry-run takes no grant and so previews exactly that -- read_project_paths says
    # why on stderr.
    with contextlib.nullcontext() if dry_run else core.projects_db.enable_writes():
        dsp.spin_while(
            label=CLEANUP_LABEL, message="scanning…", done_message=lambda: None, work=survey
        )
    scope = scoped[0]
    image_scope = image_scoped[0]

    if scope.is_empty and image_scope.is_empty:
        dsp.info(
            "Nothing to clean up: no orphaned logs, stale registry entries or outdated "
            "images found."
        )
        if image_scope.unattributable:
            dsp.info(UNATTRIBUTABLE_NOTE.format(count=image_scope.unattributable))
        ctx.exit(0)

    _CleanupReport.preview(scope, image_scope, dsp)

    if dry_run:
        ctx.exit(0)

    # Non-interactive stdin declines via prompt_confirm's EOFError handling.
    if not dsp.prompt_confirm("Proceed? [y/N]"):
        dsp.info("Cleanup cancelled.")
        ctx.exit(0)

    outcomes: list[CleanupOutcome] = []
    image_outcomes: list[ImageCleanupOutcome] = []

    def clean() -> None:
        """
        Remove the images first, then the logs and the registry.

        Image removal cannot half-succeed the way the usage archive can, so putting it
        first leaves the archive's own abort path alone: that path returns early on a
        failed promotion, and the images are already dealt with by then rather than
        skipped because of it.
        """
        image_outcomes.append(build.remove_images(image_scope))
        outcomes.append(stats.run_cleanup(scope))

    # A second grant, constructed fresh rather than reusing the one above: a
    # @contextmanager instance is single-use, and nullcontext is not, so a shared
    # variable would work under --dry-run and break on every real run. Unconditional
    # because the --dry-run exit is above -- and needed because `clean` prunes the stale
    # registry entries. Without it prune_stale_projects suppresses the refusal, returns
    # the paths anyway, and the summary reports entries as removed that are still there.
    with core.projects_db.enable_writes():
        dsp.spin_while(
            label=CLEANUP_LABEL, message="cleaning up…", done_message=lambda: None, work=clean
        )
    ctx.exit(_CleanupReport.summarize(outcomes[0], image_outcomes[0], dsp))
