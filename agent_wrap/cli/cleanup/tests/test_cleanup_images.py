# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for the image half of `agent cleanup` — preview, prompt, and reporting."""

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.cli.cleanup.constants import (
    CLEANUP_LABEL,
    SKIPPED_IMAGE_NOTE,
    STALE_REBUILD_NOTE,
    UNATTRIBUTABLE_NOTE,
)
from agent_wrap.cli.cleanup.run import _CleanupReport
from agent_wrap.cli.cleanup.run import run as cleanup_run
from agent_wrap.containers import services
from agent_wrap.domain.build.constants import ImageCleanupReason
from agent_wrap.domain.build.models import (
    ImageCleanupOutcome,
    ImageCleanupScope,
    RemovableImage,
)
from agent_wrap.domain.display.constants import TERM_WIDTH_ENV
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.stats.models import CleanupOutcome, CleanupResult, CleanupScope

if TYPE_CHECKING:
    from unittest.mock import Mock

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Return *text* without the colour escapes, so a rendered width can be measured."""
    return _ANSI_RE.sub("", text)


def _image(
    ref: str,
    reason: ImageCleanupReason = ImageCleanupReason.SUPERSEDED,
    *,
    detail: str = "claude-agent",
    size: str = "1.2GB",
) -> RemovableImage:
    return RemovableImage(
        ref=ref, display=ref, image_id=ref, size=size, reason=reason, detail=detail
    )


def _empty_stats_scope() -> CleanupScope:
    """Return a scope with nothing to clean on the logs side, so images are the whole story."""
    return CleanupScope(orphaned_dirs=[], stale_paths=[], freed_estimate=0)


def _stats_outcome() -> CleanupOutcome:
    return CleanupOutcome(
        result=CleanupResult(
            removed=0,
            freed_bytes=0,
            archive_path=Path("/wrap/.agent-launches/orphaned-usage-archive.json"),
            staging_path=Path("/wrap/.agent-launches/orphaned-usage-archive.new.json"),
            finalized=True,
        ),
        removed_paths=[],
    )


@pytest.fixture
def stats_mock() -> Mock:
    """Return the mocked StatsService with nothing to clean on the logs side."""
    stats = services.stats_service
    stats.cleanup_scope.return_value = _empty_stats_scope()  # pyrefly: ignore [missing-attribute]
    stats.run_cleanup.return_value = _stats_outcome()  # pyrefly: ignore [missing-attribute]
    return stats


@pytest.fixture
def build_mock() -> Mock:
    """Return the mocked BuildService, seeded with one removable image that removes cleanly."""
    build = services.build_service
    scope = ImageCleanupScope(images=[_image("w01")], unattributable=0)
    build.image_cleanup_scope.return_value = scope  # pyrefly: ignore [missing-attribute]
    build.remove_images.return_value = ImageCleanupOutcome(  # pyrefly: ignore [missing-attribute]
        removed=[_image("w01")], skipped=[]
    )
    return build


@pytest.fixture
def display_mock_service() -> Mock:
    """Return the mocked DisplayService, with formatters producing marked strings."""
    dsp = services.display_service
    dsp.format_bytes.side_effect = lambda n: f"<{n}B>"  # pyrefly: ignore [missing-attribute]
    dsp.spin_while.side_effect = lambda **kw: kw["work"]()  # pyrefly: ignore [missing-attribute]
    dsp.terminal_width.return_value = None  # pyrefly: ignore [missing-attribute]
    # Rendered as plain "cell | cell" lines: the borders and widths are DisplayService's own
    # tested concern, and what these tests assert is which rows and headings reach the table.
    dsp.render_table.side_effect = (  # pyrefly: ignore [missing-attribute]
        lambda title, *args, **_kwargs: [
            title,
            *[" | ".join(item.cells) if not isinstance(item, str) else "---" for item in args[2]],
        ]
    )
    return dsp


def _stdout(dsp: Mock) -> str:
    """Join every info/success/error/warning message the command emitted."""
    calls = [
        *dsp.info.call_args_list,
        *dsp.success.call_args_list,
        *dsp.error.call_args_list,
        *dsp.warning.call_args_list,
    ]
    return "\n".join(str(c[0][0]) for c in calls if c[0])


@pytest.mark.usefixtures("stats_mock", "build_mock", "display_mock_service")
def test_image_scope_is_surveyed_against_the_registry() -> None:
    """The sweep needs every registered project to know which image names are claimed."""
    services.config_service.read_project_paths.return_value = [  # pyrefly: ignore [missing-attribute]
        Path("/home/u/proj-web")
    ]

    assert cleanup_run(["--dry-run"]) == 0
    services.build_service.image_cleanup_scope.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        [Path("/home/u/proj-web")]
    )


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_images_alone_are_enough_to_run(display_mock_service: Mock) -> None:
    """Nothing on the logs side must not read as nothing to do."""
    assert cleanup_run(["--dry-run"]) == 0
    out = _stdout(display_mock_service)
    assert "Outdated images (1):" in out
    assert "project log(s) will be deleted" not in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_empty_on_both_sides_reports_nothing_to_do(display_mock_service: Mock) -> None:
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(images=[], unattributable=0)
    )

    assert cleanup_run([]) == 0
    assert "Nothing to clean up" in _stdout(display_mock_service)
    services.build_service.remove_images.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_unattributable_images_are_reported_but_never_removed(display_mock_service: Mock) -> None:
    """A pre-label leftover cannot be attributed, so the note points at `docker image prune`."""
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(images=[], unattributable=4)
    )

    assert cleanup_run([]) == 0
    out = _stdout(display_mock_service)
    assert "Nothing to clean up" in out
    assert UNATTRIBUTABLE_NOTE.format(count=4) in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_unattributable_note_also_rides_along_with_a_real_scope(
    display_mock_service: Mock,
) -> None:
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(images=[_image("w01")], unattributable=2)
    )

    assert cleanup_run(["--dry-run"]) == 0
    assert UNATTRIBUTABLE_NOTE.format(count=2) in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_dry_run_removes_nothing_and_never_prompts(display_mock_service: Mock) -> None:
    assert cleanup_run(["--dry-run"]) == 0
    services.build_service.remove_images.assert_not_called()  # pyrefly: ignore [missing-attribute]
    display_mock_service.prompt_confirm.assert_not_called()


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_declining_the_prompt_removes_no_image(display_mock_service: Mock) -> None:
    """One confirmation covers both halves, so declining it must leave images alone too."""
    display_mock_service.prompt_confirm.return_value = False

    assert cleanup_run([]) == 0
    services.build_service.remove_images.assert_not_called()  # pyrefly: ignore [missing-attribute]
    services.stats_service.run_cleanup.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_one_confirmation_covers_both_halves(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = True

    assert cleanup_run([]) == 0
    assert display_mock_service.prompt_confirm.call_count == 1
    services.build_service.remove_images.assert_called_once()  # pyrefly: ignore [missing-attribute]
    services.stats_service.run_cleanup.assert_called_once()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_images_are_removed_before_the_logs(display_mock_service: Mock) -> None:
    """
    Ordering is deliberate: the archive's abort path returns early, and images must be
    dealt with by then rather than skipped because of it.
    """
    display_mock_service.prompt_confirm.return_value = True
    order: list[str] = []
    services.build_service.remove_images.side_effect = (  # pyrefly: ignore [missing-attribute]
        lambda _scope: order.append("images") or ImageCleanupOutcome(removed=[], skipped=[])
    )
    services.stats_service.run_cleanup.side_effect = (  # pyrefly: ignore [missing-attribute]
        lambda _scope: order.append("logs") or _stats_outcome()
    )

    assert cleanup_run([]) == 0
    assert order == ["images", "logs"]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_remove_images_acts_on_the_surveyed_scope(display_mock_service: Mock) -> None:
    """No re-survey between the preview the user confirmed and the removal."""
    display_mock_service.prompt_confirm.return_value = True
    surveyed = ImageCleanupScope(images=[_image("w01")], unattributable=0)
    services.build_service.image_cleanup_scope.return_value = surveyed  # pyrefly: ignore [missing-attribute]

    assert cleanup_run([]) == 0
    services.build_service.remove_images.assert_called_once_with(surveyed)  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_success_message_counts_the_removed_images(display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = True
    services.build_service.remove_images.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupOutcome(removed=[_image("w01"), _image("g01")], skipped=[])
    )

    assert cleanup_run([]) == 0
    assert "2 image(s) removed" in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_a_refused_removal_warns_without_failing(display_mock_service: Mock) -> None:
    """`remove_images` never forces, so docker's refusal is reported, not fatal."""
    display_mock_service.prompt_confirm.return_value = True
    services.build_service.remove_images.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupOutcome(removed=[], skipped=[_image("claude-agent-api:latest")])
    )

    assert cleanup_run([]) == 0
    out = _stdout(display_mock_service)
    assert f"claude-agent-api:latest: {SKIPPED_IMAGE_NOTE}" in out
    assert "0 image(s) removed" in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_unfinalized_archive_still_reports_the_images_it_removed(
    display_mock_service: Mock,
) -> None:
    """Images go first, so an archive failure must not hide what already happened."""
    display_mock_service.prompt_confirm.return_value = True
    services.stats_service.run_cleanup.return_value = CleanupOutcome(  # pyrefly: ignore [missing-attribute]
        result=CleanupResult(
            removed=0,
            freed_bytes=0,
            archive_path=Path("/wrap/archive.json"),
            staging_path=Path("/wrap/archive.new.json"),
            finalized=False,
        ),
        removed_paths=[],
    )
    services.build_service.remove_images.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupOutcome(removed=[], skipped=[_image("w01")])
    )

    assert cleanup_run([]) == 1
    out = _stdout(display_mock_service)
    assert SKIPPED_IMAGE_NOTE in out
    assert "failed to finalize the usage archive" in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_preview_groups_rows_by_reason_with_a_heading_each(display_mock_service: Mock) -> None:
    """
    The four reasons cost the reader different things, and the stale heading is where the
    rebuild they buy gets stated once.
    """
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(
            images=[
                _image("w01"),
                _image("claude-agent-gone:latest", ImageCleanupReason.ORPHANED, detail="x"),
                _image("claude-agent-api:latest", ImageCleanupReason.STALE, detail="base moved"),
            ],
            unattributable=0,
        )
    )

    assert cleanup_run(["--dry-run"]) == 0
    out = _stdout(display_mock_service)
    assert "Outdated images (3):" in out
    assert "1 superseded build(s)" in out
    assert "1 orphaned project image(s)" in out
    assert "next 'agent run'" in out
    assert "base moved" in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_preview_shows_each_size_and_never_a_total(display_mock_service: Mock) -> None:
    """Images share layers, so a summed figure would overstate the reclaim badly."""
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(
            images=[_image("w01", size="1.2GB"), _image("a01", size="2.4GB")], unattributable=0
        )
    )

    assert cleanup_run(["--dry-run"]) == 0
    out = _stdout(display_mock_service)
    assert "1.2GB" in out
    assert "2.4GB" in out
    assert "3.6GB" not in out


@pytest.mark.parametrize("columns", [200, 60], ids=["wide", "narrow"])
def test_image_table_renders_through_the_real_display_service(
    monkeypatch: pytest.MonkeyPatch, columns: int
) -> None:
    """
    Render with the real DisplayService, at a wide and a narrow console.

    The other tests here stub `render_table` to assert which rows reach it, which cannot
    catch a width contract broken on the way in — `leading` plus the shared column count
    has to add up to every header, or the renderer indexes off the end of its widths.
    """
    monkeypatch.setenv(TERM_WIDTH_ENV, str(columns))
    scope = ImageCleanupScope(
        images=[
            _image("a01aaaaaaaaa", detail="claude-agent"),
            _image("claude-agent-gone:latest", ImageCleanupReason.ORPHANED, detail="x"),
            _image(
                "claude-agent-api:latest",
                ImageCleanupReason.STALE,
                detail="the base image claude-agent is not the one it was built on",
            ),
        ],
        unattributable=0,
    )

    lines = _CleanupReport.image_table(scope, DisplayService())

    assert lines[0] == "Outdated images (3):"
    # Every rendered row is one box-drawn line of the same width, headings included.
    widths = {len(_strip_ansi(line)) for line in lines[1:]}
    assert len(widths) == 1
    assert widths.pop() <= columns


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_stale_rows_carry_the_rebuild_note(display_mock_service: Mock) -> None:
    """The one line in the preview that says what confirming costs, rather than reclaims."""
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(
            images=[_image("claude-agent-api:latest", ImageCleanupReason.STALE, detail="moved")],
            unattributable=0,
        )
    )

    assert cleanup_run(["--dry-run"]) == 0
    assert STALE_REBUILD_NOTE in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_rebuild_note_is_absent_without_a_stale_row(display_mock_service: Mock) -> None:
    """A superseded build costs nothing, so nothing should warn about a rebuild."""
    assert cleanup_run(["--dry-run"]) == 0
    assert STALE_REBUILD_NOTE not in _stdout(display_mock_service)


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_both_surveys_run_under_one_scan_spinner(display_mock_service: Mock) -> None:
    """One scan, one spinner: the two surveys are both read-only and both happen up front."""
    assert cleanup_run(["--dry-run"]) == 0
    labels = [call.kwargs["label"] for call in display_mock_service.spin_while.call_args_list]
    messages = [call.kwargs["message"] for call in display_mock_service.spin_while.call_args_list]
    assert labels == [CLEANUP_LABEL]
    assert messages == ["scanning…"]
    services.stats_service.cleanup_scope.assert_called_once()  # pyrefly: ignore [missing-attribute]
    services.build_service.image_cleanup_scope.assert_called_once()  # pyrefly: ignore [missing-attribute]
