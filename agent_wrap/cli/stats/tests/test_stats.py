# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for the `stats` subcommand — rendering and arg parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.cli.stats.complete import complete as stats_complete
from agent_wrap.cli.stats.display import render, render_source_breakdown
from agent_wrap.cli.stats.run import run as stats_run
from agent_wrap.constants import ORPHANED_LABEL
from agent_wrap.containers import services
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.stats.models import StatsReport

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
def wired_services(tmp_path: Path) -> None:
    """Seed the mocked services so `run()` reaches the render call."""
    services.config_service.read_project_paths.return_value = [tmp_path / "proj"]  # type: ignore[union-attr]
    services.stats_service.resolve_window.return_value = (None, None)  # type: ignore[union-attr]
    services.stats_service.build_report.return_value = _report()  # type: ignore[union-attr]


def _report(
    *, rows: list[Any] | None = None, orphaned: object = None, unrecorded: int = 0
) -> StatsReport:
    return StatsReport(
        rows=rows or [],
        totals_by_model={},
        totals_by_day_by_model={},
        totals_by_source={},
        orphaned=orphaned,  # type: ignore[arg-type]
        unrecorded=unrecorded,
    )


def _orphaned_result(msgs: int) -> dict[str, object]:
    return {"sessions": 0, "last_ts": None, "total": _source_bucket(msgs, in_=10)}


def _project_row() -> dict[str, Any]:
    return {
        "path": Path("/proj"),
        "exists": True,
        "sessions": 1,
        "last_ts": None,
        "total": _source_bucket(1),
        "cost": 0.0,
    }


@pytest.mark.usefixtures("wired_services")
def test_run_renders_the_reports_orphaned_row(mocker: MockerFixture) -> None:
    """Whatever orphaned row the report carries is what render() is handed."""
    merged = _orphaned_result(3)
    services.stats_service.build_report.return_value = _report(orphaned=merged)  # type: ignore[union-attr]
    render_spy = mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run([]) == 0
    assert render_spy.call_args.kwargs["orphaned"] is merged


@pytest.mark.usefixtures("wired_services")
def test_run_renders_orphaned_only_state(mocker: MockerFixture) -> None:
    """
    A report with no project rows but an orphaned row must still render.

    The early "no logs found" return keys off both, so an archive-only state
    (everything already cleaned up) has to survive it.
    """
    archived = _orphaned_result(2)
    services.stats_service.build_report.return_value = _report(orphaned=archived)  # type: ignore[union-attr]
    render_spy = mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run([]) == 0
    render_spy.assert_called_once()
    services.display_service.error.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.usefixtures("wired_services")
def test_run_errors_when_report_is_empty(mocker: MockerFixture) -> None:
    render_spy = mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run([]) == 0
    render_spy.assert_not_called()
    message = services.display_service.error.call_args[0][0]  # type: ignore[union-attr]
    assert "no LiteLLM logs found" in message


@pytest.mark.usefixtures("wired_services")
def test_run_names_the_pattern_when_it_matched_nothing(mocker: MockerFixture) -> None:
    """An empty report under a pattern must say so, not blame the whole registry."""
    mocker.patch("agent_wrap.cli.stats.run.render", return_value="")

    assert stats_run(["-p", "nomatch"]) == 0
    message = services.display_service.error.call_args[0][0]  # type: ignore[union-attr]
    assert "nomatch" in message


@pytest.mark.usefixtures("wired_services")
def test_run_passes_the_parsed_window_to_the_report(mocker: MockerFixture) -> None:
    mocker.patch("agent_wrap.cli.stats.run.render", return_value="")
    services.stats_service.resolve_window.return_value = ("2026-07-01", "2026-07-20")  # type: ignore[union-attr]
    services.stats_service.build_report.return_value = _report(rows=[_project_row()])  # type: ignore[union-attr]

    assert stats_run(["--from", "2026-07-01", "--until", "2026-07-20"]) == 0
    _projects, args = services.stats_service.build_report.call_args.args  # type: ignore[union-attr]
    assert (args.from_iso, args.until_iso) == ("2026-07-01", "2026-07-20")


@pytest.mark.usefixtures("wired_services")
def test_run_footnotes_unrecorded_usage(mocker: MockerFixture) -> None:
    mocker.patch("agent_wrap.cli.stats.run.render", return_value="")
    services.stats_service.build_report.return_value = _report(  # type: ignore[union-attr]
        rows=[_project_row()], unrecorded=4
    )

    assert stats_run([]) == 0
    warning = services.display_service.warning.call_args[0][0]  # type: ignore[union-attr]
    assert "4 successful request(s) had unrecorded usage" in warning


@pytest.mark.usefixtures("wired_services")
def test_run_errors_when_no_projects_registered() -> None:
    services.config_service.read_project_paths.return_value = []  # type: ignore[union-attr]

    assert stats_run([]) == 0
    message = services.display_service.error.call_args[0][0]  # type: ignore[union-attr]
    assert "no projects recorded yet" in message


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
