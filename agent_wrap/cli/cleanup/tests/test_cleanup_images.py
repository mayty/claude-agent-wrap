# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for the image half of `agent cleanup` — preview, prompt, and reporting."""

import contextlib
import io
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli.cleanup.constants import (
    CLEANUP_LABEL,
    SKIPPED_IMAGE_NOTE,
    STALE_REBUILD_NOTE,
    UNATTRIBUTABLE_NOTE,
)
from agent_wrap.cli.cleanup.run import _CleanupReport
from agent_wrap.containers import services
from agent_wrap.domain.build.constants import ImageCleanupReason
from agent_wrap.domain.build.models import (
    ImageCleanupOutcome,
    ImageCleanupScope,
    RemovableImage,
)
from agent_wrap.domain.display.constants import TERM_WIDTH_ENV
from agent_wrap.domain.stats.models import CleanupOutcome, CleanupResult, CleanupScope

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import Mock

    from click.testing import CliRunner
    from rich.console import RenderableType

    from agent_wrap.domain.display.service import DisplayService


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
    return CleanupOutcome(result=CleanupResult(removed=0, freed_bytes=0), removed_paths=[])


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
def display_mock_service(non_tty_display: DisplayService) -> Mock:
    """Return the mocked DisplayService, with formatters producing marked strings."""
    dsp = services.display_service
    dsp.format_bytes.side_effect = lambda n: f"<{n}B>"  # pyrefly: ignore [missing-attribute]
    dsp.spin_while.side_effect = lambda **kw: kw["work"]()  # pyrefly: ignore [missing-attribute]
    dsp.render_table.side_effect = non_tty_display.render_table  # pyrefly: ignore
    return dsp


@pytest.fixture
def shown(non_tty_display: DisplayService) -> Callable[[RenderableType], list[str]]:
    """Return the lines `show` puts on stdout for a renderable."""

    def _shown(renderable: RenderableType) -> list[str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            non_tty_display.show(renderable)
        return buffer.getvalue().splitlines()

    return _shown


@pytest.fixture
def stdout(
    display_mock_service: Mock, shown: Callable[[RenderableType], list[str]]
) -> Callable[[], str]:
    """
    Return everything the command put on the terminal, with its tables drawn.

    `mock_calls` rather than the per-method lists, so a table keeps its place among the
    prose lines either side of it.
    """

    def _stdout() -> str:
        out: list[str] = []
        for name, args, _kwargs in display_mock_service.mock_calls:
            if name == "show":
                out.extend(shown(args[0]))
            elif name in {"info", "success", "error", "warning"} and args:
                out.append(str(args[0]))
        return "\n".join(out)

    return _stdout


@pytest.mark.usefixtures("stats_mock", "build_mock", "display_mock_service")
def test_image_scope_is_surveyed_against_the_registry(runner: CliRunner) -> None:
    """The sweep needs every registered project to know which image names are claimed."""
    services.config_service.read_project_paths.return_value = [  # pyrefly: ignore [missing-attribute]
        Path("/home/u/proj-web")
    ]

    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    services.build_service.image_cleanup_scope.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        [Path("/home/u/proj-web")]
    )


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_images_alone_are_enough_to_run(runner: CliRunner, stdout: Callable[[], str]) -> None:
    """Nothing on the logs side must not read as nothing to do."""
    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    out = stdout()
    assert "Outdated images (1):" in out
    assert "project log(s) will be deleted" not in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_empty_on_both_sides_reports_nothing_to_do(
    runner: CliRunner, stdout: Callable[[], str]
) -> None:
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(images=[], unattributable=0)
    )

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "Nothing to clean up" in stdout()
    services.build_service.remove_images.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_unattributable_images_are_reported_but_never_removed(
    runner: CliRunner, stdout: Callable[[], str]
) -> None:
    """A pre-label leftover cannot be attributed, so the note points at `docker image prune`."""
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(images=[], unattributable=4)
    )

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = stdout()
    assert "Nothing to clean up" in out
    assert UNATTRIBUTABLE_NOTE.format(count=4) in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_unattributable_note_also_rides_along_with_a_real_scope(
    runner: CliRunner,
    stdout: Callable[[], str],
) -> None:
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(images=[_image("w01")], unattributable=2)
    )

    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    assert UNATTRIBUTABLE_NOTE.format(count=2) in stdout()


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_dry_run_removes_nothing_and_never_prompts(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    services.build_service.remove_images.assert_not_called()  # pyrefly: ignore [missing-attribute]
    display_mock_service.prompt_confirm.assert_not_called()


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_declining_the_prompt_removes_no_image(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """One confirmation covers both halves, so declining it must leave images alone too."""
    display_mock_service.prompt_confirm.return_value = False

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    services.build_service.remove_images.assert_not_called()  # pyrefly: ignore [missing-attribute]
    services.stats_service.run_cleanup.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_one_confirmation_covers_both_halves(runner: CliRunner, display_mock_service: Mock) -> None:
    display_mock_service.prompt_confirm.return_value = True

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert display_mock_service.prompt_confirm.call_count == 1
    services.build_service.remove_images.assert_called_once()  # pyrefly: ignore [missing-attribute]
    services.stats_service.run_cleanup.assert_called_once()  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_images_are_removed_before_the_logs(runner: CliRunner, display_mock_service: Mock) -> None:
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

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert order == ["images", "logs"]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_remove_images_acts_on_the_surveyed_scope(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """No re-survey between the preview the user confirmed and the removal."""
    display_mock_service.prompt_confirm.return_value = True
    surveyed = ImageCleanupScope(images=[_image("w01")], unattributable=0)
    services.build_service.image_cleanup_scope.return_value = surveyed  # pyrefly: ignore [missing-attribute]

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    services.build_service.remove_images.assert_called_once_with(surveyed)  # pyrefly: ignore [missing-attribute]


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_success_message_counts_the_removed_images(
    runner: CliRunner, display_mock_service: Mock, stdout: Callable[[], str]
) -> None:
    display_mock_service.prompt_confirm.return_value = True
    services.build_service.remove_images.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupOutcome(removed=[_image("w01"), _image("g01")], skipped=[])
    )

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    assert "2 image(s) removed" in stdout()


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_a_refused_removal_warns_without_failing(
    runner: CliRunner, display_mock_service: Mock, stdout: Callable[[], str]
) -> None:
    """`remove_images` never forces, so docker's refusal is reported, not fatal."""
    display_mock_service.prompt_confirm.return_value = True
    services.build_service.remove_images.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupOutcome(removed=[], skipped=[_image("claude-agent-api:latest")])
    )

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = stdout()
    assert f"claude-agent-api:latest: {SKIPPED_IMAGE_NOTE}" in out
    assert "0 image(s) removed" in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_a_log_dir_that_survived_still_reports_the_images_it_removed(
    runner: CliRunner,
    display_mock_service: Mock,
    stdout: Callable[[], str],
) -> None:
    """
    Images go first, so a log dir that could not be deleted must not hide their removal.

    The run also stays successful. A dir that survives is simply still orphaned next
    time, unlike the old half-committed archive, which was a state only the user could
    repair and so exited non-zero.
    """
    display_mock_service.prompt_confirm.return_value = True
    services.stats_service.run_cleanup.return_value = CleanupOutcome(  # pyrefly: ignore [missing-attribute]
        result=CleanupResult(removed=0, freed_bytes=0),
        removed_paths=[],
    )
    services.build_service.remove_images.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupOutcome(removed=[], skipped=[_image("w01")])
    )

    assert runner.invoke(cli_root, ["cleanup"]).exit_code == 0
    out = stdout()
    assert SKIPPED_IMAGE_NOTE in out
    assert "0 project log(s) deleted" in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_preview_groups_rows_by_reason_with_a_heading_each(
    runner: CliRunner, stdout: Callable[[], str]
) -> None:
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

    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    out = stdout()
    assert "Outdated images (3):" in out
    assert "1 superseded build(s)" in out
    assert "1 orphaned project image(s)" in out
    assert "next 'agent run'" in out
    assert "base moved" in out


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_preview_shows_each_size_and_never_a_total(
    runner: CliRunner, stdout: Callable[[], str]
) -> None:
    """Images share layers, so a summed figure would overstate the reclaim badly."""
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(
            images=[_image("w01", size="1.2GB"), _image("a01", size="2.4GB")], unattributable=0
        )
    )

    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    out = stdout()
    assert "1.2GB" in out
    assert "2.4GB" in out
    assert "3.6GB" not in out


@pytest.mark.parametrize("columns", [200, 60], ids=["wide", "narrow"])
def test_image_table_renders_through_the_real_display_service(
    monkeypatch: pytest.MonkeyPatch,
    columns: int,
    non_tty_display: DisplayService,
    shown: Callable[[RenderableType], list[str]],
) -> None:
    """
    Render with the real DisplayService, at a wide and a narrow console.

    The other tests here read the rendered text for the rows and headings they expect,
    which cannot catch a width contract broken on the way in — `leading` plus the shared
    column count has to add up to every header, or the renderer indexes off the end of
    its widths.
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

    lines = shown(_CleanupReport.image_table(scope, non_tty_display))

    assert lines[0] == "Outdated images (3):"
    # Every rendered row is one box-drawn line of the same width, headings included.
    widths = {len(line) for line in lines[1:]}
    assert len(widths) == 1
    assert widths.pop() <= columns


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_stale_rows_carry_the_rebuild_note(runner: CliRunner, stdout: Callable[[], str]) -> None:
    """The one line in the preview that says what confirming costs, rather than reclaims."""
    services.build_service.image_cleanup_scope.return_value = (  # pyrefly: ignore [missing-attribute]
        ImageCleanupScope(
            images=[_image("claude-agent-api:latest", ImageCleanupReason.STALE, detail="moved")],
            unattributable=0,
        )
    )

    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    assert STALE_REBUILD_NOTE in stdout()


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_rebuild_note_is_absent_without_a_stale_row(
    runner: CliRunner,
    stdout: Callable[[], str],
) -> None:
    """A superseded build costs nothing, so nothing should warn about a rebuild."""
    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    assert STALE_REBUILD_NOTE not in stdout()


@pytest.mark.usefixtures("stats_mock", "build_mock")
def test_both_surveys_run_under_one_scan_spinner(
    runner: CliRunner, display_mock_service: Mock
) -> None:
    """One scan, one spinner: the two surveys are both read-only and both happen up front."""
    assert runner.invoke(cli_root, ["cleanup", "--dry-run"]).exit_code == 0
    labels = [call.kwargs["label"] for call in display_mock_service.spin_while.call_args_list]
    messages = [call.kwargs["message"] for call in display_mock_service.spin_while.call_args_list]
    assert labels == [CLEANUP_LABEL]
    assert messages == ["scanning…"]
    services.stats_service.cleanup_scope.assert_called_once()  # pyrefly: ignore [missing-attribute]
    services.build_service.image_cleanup_scope.assert_called_once()  # pyrefly: ignore [missing-attribute]
