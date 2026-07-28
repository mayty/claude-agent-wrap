# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for the `stats` subcommand — rendering and arg parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.cli.stats.complete import complete as stats_complete
from agent_wrap.cli.stats.display import render, render_source_breakdown
from agent_wrap.cli.stats.run import run as stats_run
from agent_wrap.cli.stats.usage_args import parse_usage_args
from agent_wrap.containers import services
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.stats.constants import ORPHANED_LABEL
from agent_wrap.domain.stats.models import AggregateResult

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.fixture
def display_service() -> Mock:
    """Mock DisplayService that delegates formatting to the real implementation."""
    return Mock(spec=DisplayService, wraps=DisplayService())


def _source_bucket(msgs: int, *, in_: int = 0) -> Bucket:
    b = Bucket()
    for _ in range(msgs):
        b.add(
            {
                "input_tokens": in_,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation": {},
            },
            0.0,
        )
    return b


def test_render_includes_orphaned_row(display_service: Mock) -> None:
    """render() shows an <orphaned> row (accented in color, no text marker)."""
    b = Bucket()
    b.add(
        {
            "input_tokens": 1000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        0.0,
    )
    b.add(
        {
            "input_tokens": 1000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        0.0,
    )
    last_ts = datetime(2026, 6, 29, tzinfo=timezone.utc)
    orphaned = {"sessions": 1, "last_ts": last_ts, "total": b}
    out = render([], {}, None, None, orphaned=orphaned, display=display_service)
    assert ORPHANED_LABEL in out
    assert f"{ORPHANED_LABEL} *" not in out


def test_render_without_orphaned_has_no_row(display_service: Mock) -> None:
    """When orphaned is None, no <orphaned> row appears."""
    out = render([], {}, None, None, orphaned=None, display=display_service)
    assert ORPHANED_LABEL not in out


def test_render_source_breakdown_lists_active_sources(display_service: Mock) -> None:
    by_source = {
        "native": {"bedrock/claude-opus-4-8": _source_bucket(3, in_=1000)},
        "standard_logging_object": {"bedrock/claude-opus-4-8": _source_bucket(2, in_=500)},
        "unrecoverable": {"bedrock/claude-opus-4-8": _source_bucket(1)},
    }
    out = render_source_breakdown(by_source, None, None, display=display_service)
    assert "Usage source breakdown (all time):" in out
    assert "native" in out
    assert "standard_logging_object" in out
    assert "unrecoverable" in out
    assert "TOTAL" in out


def test_render_source_breakdown_omits_zero_msg_sources(display_service: Mock) -> None:
    by_source = {"native": {"bedrock/claude-opus-4-8": _source_bucket(2, in_=100)}}
    out = render_source_breakdown(by_source, None, None, display=display_service)
    assert "native" in out
    assert "standard_logging_object" not in out


def test_render_source_breakdown_empty_when_no_activity(display_service: Mock) -> None:
    assert render_source_breakdown({}, None, None, display=display_service) == ""


def test_render_source_breakdown_merges_across_models(display_service: Mock) -> None:
    by_source = {
        "unrecoverable": {"bedrock/claude-opus-4-8": _source_bucket(1)},
        "native": {"bedrock/claude-haiku-4-5": _source_bucket(1, in_=1)},
    }
    out = render_source_breakdown(by_source, "2026-06-01", "2026-06-29", display=display_service)
    assert "Usage source breakdown (2026-06-01 … 2026-06-29):" in out
    assert "unrecoverable" in out
    assert "native" in out


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """Write a registry file where cli.stats.run expects it, holding one project."""
    launches = tmp_path / ".agent-launches"
    launches.mkdir(exist_ok=True)
    projects_file = launches / "projects.txt"
    projects_file.write_text(f"{tmp_path / 'proj'}\n", encoding="utf-8")
    return projects_file


@pytest.fixture
def wired_services(registry: Path, tmp_path: Path) -> None:
    """Seed the mocked services so `run()` reaches the render call."""
    _ = registry
    services.config_service.read_project_paths.return_value = [tmp_path / "proj"]  # type: ignore[union-attr]
    services.stats_service.orphaned_log_dirs.return_value = []  # type: ignore[union-attr]
    services.stats_service.scan_log_dirs.return_value = {}  # type: ignore[union-attr]
    services.stats_service.aggregate_projects.return_value = AggregateResult([], {}, {}, {})  # type: ignore[union-attr]
    services.stats_service.aggregate_orphaned.return_value = None  # type: ignore[union-attr]
    services.stats_service.aggregate_archived_orphaned.return_value = None  # type: ignore[union-attr]
    services.stats_service.merge_orphaned_results.return_value = None  # type: ignore[union-attr]


def _orphaned_result(msgs: int) -> dict[str, object]:
    return {"sessions": 0, "last_ts": None, "total": _source_bucket(msgs, in_=10)}


@pytest.mark.usefixtures("wired_services")
def test_run_merges_archived_into_orphaned_row(mocker: MockerFixture) -> None:
    """Both orphaned sources are combined, and the merged result reaches render()."""
    live = _orphaned_result(1)
    archived = _orphaned_result(2)
    merged = _orphaned_result(3)
    services.stats_service.aggregate_orphaned.return_value = live  # type: ignore[union-attr]
    services.stats_service.aggregate_archived_orphaned.return_value = archived  # type: ignore[union-attr]
    services.stats_service.merge_orphaned_results.return_value = merged  # type: ignore[union-attr]
    render_spy = mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run([]) == 0
    services.stats_service.merge_orphaned_results.assert_called_once_with(live, archived)  # type: ignore[union-attr]
    assert render_spy.call_args.kwargs["orphaned"] is merged


@pytest.mark.usefixtures("wired_services")
def test_run_renders_archive_only_state(mocker: MockerFixture) -> None:
    """
    With no projects and no live orphans, a non-empty archive must still render.

    The early "no logs found" return keys off the merged result, so an archive-only
    state (everything already cleaned up) has to survive it.
    """
    archived = _orphaned_result(2)
    services.stats_service.aggregate_orphaned.return_value = None  # type: ignore[union-attr]
    services.stats_service.aggregate_archived_orphaned.return_value = archived  # type: ignore[union-attr]
    services.stats_service.merge_orphaned_results.return_value = archived  # type: ignore[union-attr]
    render_spy = mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run([]) == 0
    render_spy.assert_called_once()
    assert render_spy.call_args.kwargs["orphaned"] is archived
    services.display_service.error.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.usefixtures("wired_services")
def test_run_errors_when_nothing_anywhere(mocker: MockerFixture) -> None:
    render_spy = mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run([]) == 0
    render_spy.assert_not_called()
    message = services.display_service.error.call_args[0][0]  # type: ignore[union-attr]
    assert "no LiteLLM logs found" in message


@pytest.mark.usefixtures("wired_services")
def test_run_skips_archived_when_pattern_excludes_orphaned(mocker: MockerFixture) -> None:
    """Both orphaned calls share the pattern gate — they fold into the same totals."""
    mocker.patch("agent_wrap.cli.stats.run.render", return_value="")
    services.stats_service.aggregate_projects.return_value = AggregateResult(  # type: ignore[union-attr]
        [
            {
                "path": Path("/proj"),
                "exists": True,
                "sessions": 1,
                "last_ts": None,
                "total": _source_bucket(1),
                "cost": 0.0,
            }
        ],
        {},
        {},
        {},
    )

    assert stats_run(["-p", "proj"]) == 0
    services.stats_service.aggregate_orphaned.assert_not_called()  # type: ignore[union-attr]
    services.stats_service.aggregate_archived_orphaned.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.usefixtures("wired_services")
def test_run_passes_window_to_archived_call(mocker: MockerFixture) -> None:
    mocker.patch("agent_wrap.cli.stats.run.render", return_value="")
    services.stats_service.merge_orphaned_results.return_value = _orphaned_result(1)  # type: ignore[union-attr]

    assert stats_run(["--from", "2026-07-01", "--until", "2026-07-20"]) == 0
    kwargs = services.stats_service.aggregate_archived_orphaned.call_args.kwargs  # type: ignore[union-attr]
    assert kwargs["from_iso"] == "2026-07-01"
    assert kwargs["until_iso"] == "2026-07-20"


def test_parse_usage_args_verbose_short_flag(tmp_path: Path, display_service: Mock) -> None:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args(
        [str(reg), "-v"], usage_line="u", usage_text="u", display=display_service
    )
    assert parsed is not None
    assert parsed.verbose is True


def test_parse_usage_args_verbose_long_flag(tmp_path: Path, display_service: Mock) -> None:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args(
        [str(reg), "--verbose"], usage_line="u", usage_text="u", display=display_service
    )
    assert parsed is not None
    assert parsed.verbose is True


def test_parse_usage_args_verbose_defaults_false(tmp_path: Path, display_service: Mock) -> None:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg)], usage_line="u", usage_text="u", display=display_service)
    assert parsed is not None
    assert parsed.verbose is False


def test_complete_bare_tab_shows_all_flags() -> None:
    result = stats_complete(2, ["agent", "stats", ""])
    assert "-v" in result
    assert "--verbose" in result
    assert "-f" in result
    assert "--from" in result
    assert "-p" in result
    assert "--pattern" in result


def test_complete_verbose_consumed() -> None:
    result = stats_complete(3, ["agent", "stats", "-v", ""])
    assert "-v" not in result
    assert "--verbose" not in result
    assert "-f" in result  # still available


def test_complete_shorthand_excludes_long() -> None:
    result = stats_complete(3, ["agent", "stats", "-u", ""])
    assert "-u" not in result
    assert "--until" not in result


def test_complete_value_flag_prev_returns_empty() -> None:
    result = stats_complete(3, ["agent", "stats", "-d", ""])
    assert result == []


def test_complete_after_date_value() -> None:
    result = stats_complete(4, ["agent", "stats", "-d", "14", ""])
    assert "-v" in result


def test_complete_pattern_value_prev_returns_empty() -> None:
    result = stats_complete(3, ["agent", "stats", "-p", ""])
    assert result == []
