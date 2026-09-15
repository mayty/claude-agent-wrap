# This file has been created with the assistance of an AI tool.
"""
Domain-layer tests for rolling the request index up into the render inputs.

What they assert: how projects group, how a window narrows, and where spend with no
owning project ends up. Each writes a log tree, indexes it, and asks ``StatsService`` --
see this package's ``conftest.py`` for the three fixtures that do the writing.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from agent_wrap.cli.stats.tree import build_project_tree, flatten_tree
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.constants import MARKER_NAME

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import Mock

    import pytest_mock

    from agent_wrap.conftest import FakeProvider
    from agent_wrap.domain.stats.service import StatsService

_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}


@pytest.fixture
def stats_svc(
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
    make_fake_provider: Callable[..., FakeProvider],
    make_stats_service: Callable[[PricingService], StatsService],
) -> StatsService:
    """Return a StatsService over the test's own logs database, priced from one table."""
    provider_service = mocker.Mock(spec=ProviderService)
    provider_service.get_provider.return_value = make_fake_provider(
        flat={"claude-opus-4-8": _RATES}
    )
    pricing = PricingService(provider_service=provider_service, display_service=display_mock)
    return make_stats_service(pricing)


def _rec(
    day: str = "2026-07-20", model: str = "claude-opus-4-8", **response: Any
) -> dict[str, Any]:
    """
    Build one success record landing on *day*, host-local noon.

    Noon rather than midnight so the record's stats day is the same in any timezone —
    at midnight a whole-hour offset either side of UTC lands it on the day before or
    after, and the assertions here are about days.
    """
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    return {
        "status": "success",
        "model": model,
        "timing": {
            "start": datetime.strptime(f"{day} 12:00", "%Y-%m-%d %H:%M").astimezone().timestamp()
        },
        "response": {"usage": usage, **response},
    }


def _owned(project: Path, stats_svc: StatsService) -> dict[Path, str]:
    """Return the owner map for one project, as ``build_report`` would compute it."""
    return stats_svc.project_owners([project])


def test_a_marked_group_becomes_one_row(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    """
    Two projects under one ``.agent_stats_leaf`` marker aggregate into a single row.

    Each still owns its own hash in the index — grouping is a read-time decision about
    paths, and nothing about the index knows the two are related.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / MARKER_NAME).write_text("batch-feb\n", encoding="utf-8")
    a = runs / "agent-a"
    b = runs / "agent-b"
    for project in (a, b):
        project.mkdir()
        write_session(project, "s1", [_rec()])
        link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    rows, _totals, _by_day = stats_svc.aggregate_projects(
        [a, b], cache, stats_svc.project_owners([a, b])
    )

    assert len(rows) == 1
    row = rows[0]
    assert (row["path"], row["name"], row["transient"]) == (runs, "runs", True)
    assert row["sessions"] == 2
    assert row["total"].msgs == 2


def test_an_empty_marker_still_marks_the_group_transient(  # noqa: PLR0913 -- three tree factories
    tmp_path: Path,
    stats_svc: StatsService,
    display_mock: Mock,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    """The marker's content is never read, and an empty one renders without a `` *``."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / MARKER_NAME).write_text("", encoding="utf-8")
    a = runs / "agent-a"
    b = runs / "agent-b"
    for project in (a, b):
        project.mkdir()
        write_session(project, "s1", [_rec()])
        link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    rows, _totals, _by_day = stats_svc.aggregate_projects(
        [a, b], cache, stats_svc.project_owners([a, b])
    )

    assert [row["name"] for row in rows] == ["runs"]
    display = flatten_tree(build_project_tree(rows), display=display_mock)
    group = next(dr for dr in display if dr.label.rstrip().endswith("runs"))
    assert group.transient is True
    assert " *" not in group.label


def test_unmarked_projects_stay_separate_rows(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    for project in (a, b):
        project.mkdir()
        write_session(project, "s1", [_rec()])
        link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    rows, _totals, _by_day = stats_svc.aggregate_projects(
        [a, b], cache, stats_svc.project_owners([a, b])
    )

    assert {row["name"] for row in rows} == {"proj-a", "proj-b"}
    assert all(row["transient"] is False for row in rows)


def test_a_window_narrows_sessions_and_totals_together(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    """
    An out-of-window session contributes no row count and no day bucket.

    Both halves matter: the session count comes from the distinct sessions the cells
    mention, so a window that filtered the totals but not the count would report a
    project as having two sessions and one day's spend.
    """
    project = tmp_path / "proj"
    project.mkdir()
    write_session(project, "in-window", [_rec("2026-06-15")])
    write_session(project, "out-of-window", [_rec("2026-01-01")])
    link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso="2026-06-01", until_iso="2026-06-30")
    rows, _totals, by_day = stats_svc.aggregate_projects(
        [project], cache, _owned(project, stats_svc)
    )

    assert len(rows) == 1
    assert rows[0]["sessions"] == 1
    assert rows[0]["total"].msgs == 1
    assert set(by_day) == {"2026-06-15"}


def test_only_unrecoverable_usage_counts_as_unrecorded(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    """All three outcomes are counted, but only the unrecoverable one is unrecorded."""
    project = tmp_path / "proj"
    project.mkdir()
    write_session(
        project,
        "s1",
        [
            _rec(),
            _rec(_usage_source="standard_logging_object"),
            _rec(_usage_source="unrecoverable"),
        ],
    )
    link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    _rows, totals, _by_day = stats_svc.aggregate_projects(
        [project], cache, _owned(project, stats_svc)
    )

    total = Bucket()
    for bucket in totals.values():
        total.merge(bucket)
    assert total.msgs == 3
    assert total.unrecorded == 1


def test_a_hash_no_project_claims_folds_into_the_orphan_row(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    index_logs: Callable[[], None],
) -> None:
    """
    Spend whose project is unregistered or deleted is still real, and still counted.

    Folded into the shared totals as well as into its own row, so the ``<orphaned>``
    line and the by-day table describe the same requests.
    """
    deleted = tmp_path / "was-a-project"
    deleted.mkdir()
    write_session(deleted, "s1", [_rec(), _rec()])
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}
    orphaned = stats_svc.aggregate_orphaned(cache, set(), totals_by_model, totals_by_day_by_model)

    assert orphaned is not None
    assert orphaned["sessions"] == 1
    assert orphaned["total"].msgs == 2
    assert sum(bucket.msgs for bucket in totals_by_model.values()) == 2


def test_no_orphan_row_when_every_hash_is_claimed(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    write_session(project, "s1", [_rec()])
    link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    owned = set(_owned(project, stats_svc).values())

    assert stats_svc.aggregate_orphaned(cache, owned, {}, {}) is None


def test_a_project_row_carries_the_exact_instant_not_the_hour(
    tmp_path: Path,
    stats_svc: StatsService,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> None:
    """
    ``last_ts`` is the request's own timestamp, to the second.

    The aggregate groups by UTC hour, so the tempting shortcut is to report the hour's
    start. That is wrong by up to an hour, and in a half-hour-offset timezone an hour
    straddles local midnight — the rendered "LAST" date would be a day out. Hence the
    raw ``started_at_us`` in the covering index.
    """
    project = tmp_path / "proj"
    project.mkdir()
    record = _rec()
    write_session(project, "s1", [record])
    link_project(project)
    index_logs()

    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    rows, _totals, _by_day = stats_svc.aggregate_projects(
        [project], cache, _owned(project, stats_svc)
    )

    last_ts = rows[0]["last_ts"]
    assert last_ts is not None
    assert last_ts.timestamp() == pytest.approx(record["timing"]["start"], abs=1e-6)
