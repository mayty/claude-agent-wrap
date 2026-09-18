# This file has been created with the assistance of an AI tool.
"""
A pinned end-to-end aggregation, captured from the file-scan implementation.

This exists for one purpose: the read path has moved off the JSONL files and onto
``logs.db``, and the file scan that produced these numbers is now deleted. There is no
second implementation left to diff against, so the agreement was captured *before* the
cutover, while both could still be compared — and this module is that capture.

:data:`GOLDEN_REPORT` is therefore not a hand-computed expectation. It is the output of
``StatsService.build_report`` over :func:`golden_tree`'s fixed log tree, as of the file
scan, serialized. The database-backed aggregate that replaced it reproduces it byte for
byte — including the parts that are arguably wrong.

Two of those are deliberate and worth naming, because a reader will otherwise assume
the golden is stale:

* **Every cache write is charged at the 5-minute rate.** ``Bucket.add`` splits 5m/1h
  only from ``usage.cache_creation``, which no record in the real tree populates (0 of
  45,403 successful requests with a usage block), so in practice the flat
  ``cache_creation_input_tokens`` total always falls through to the 5m rate — while
  18,133 of those records carry a genuine split one level down, at
  ``usage.prompt_tokens_details.cache_creation_token_details``. The schema stores that
  split and ``usage_from_cell`` deliberately does not spend it: correcting the rate is a
  change to what users are told they spent, and burying it inside a storage change would
  make a shifted number impossible to attribute. It gets its own change.
* **A record with no model is dropped silently**, rather than counted as unknown.

The tree covers the branches the fold actually has: all three usage-source outcomes,
unrecorded usage, a failure, a missing model, a missing timing (the ``?`` day key), two
day buckets, two providers under one project, and an orphaned directory with no owner.
It is written to disk and ingested, so what these tests read back travelled the whole
route a real request does — parser, blob store, schema, aggregate.

``totals_by_day_by_model`` is what the default table renders, so it is pinned here.
The 28-day default window is deliberately *not* pinned: it is ``resolve_window``'s date
arithmetic, which this change does not touch and which has its own tests, and pinning it
would only make the golden expire.
"""

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.models import UsageArgs

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import Mock

    import pytest_mock

    from agent_wrap.conftest import FakeProvider
    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.stats.models import ProjectRow, StatsReport
    from agent_wrap.domain.stats.service import StatsService

#: Dollars per million tokens, one model so the arithmetic is checkable by hand.
_RATES = {"in": 3.0, "out": 15.0, "cw_5m": 3.75, "cw_1h": 6.0, "cr": 0.30}

#: The two days every dated record in the tree falls on.
_DAY_ONE = "2025-03-04"
_DAY_TWO = "2025-03-05"

#: The pinned aggregation. Regenerating this by hand is a mistake -- if it needs to
#: change, the change to the fold that caused it is what needs justifying.
GOLDEN_REPORT = """\
unrecorded=2
row alpha sessions=3 msgs=7 in=9500 out=1200 cw_5m=4400 cw_1h=0 cr=1500 cost=0.045750 unknown=False unrecorded=1
row beta sessions=1 msgs=3 in=3900 out=700 cw_5m=2400 cw_1h=0 cr=0 cost=0.024000 unknown=False unrecorded=0
orphaned sessions=1 msgs=2 in=400 out=80 cw_5m=0 cw_1h=0 cr=0 cost=0.002400 unknown=False unrecorded=1
model litellm-bedrock/claude-opus-4-8 msgs=11 in=13300 out=1880 cw_5m=6800 cw_1h=0 cr=1500 cost=0.069150
model litellm-deepseek/claude-opus-4-8 msgs=1 in=500 out=100 cw_5m=0 cw_1h=0 cr=0 cost=0.003000
day 2025-03-04 litellm-bedrock/claude-opus-4-8 msgs=7 in=9300 out=1130 cw_5m=4400 cw_1h=0 cr=1500 cost=0.044100
day 2025-03-04 litellm-deepseek/claude-opus-4-8 msgs=1 in=500 out=100 cw_5m=0 cw_1h=0 cr=0 cost=0.003000
day 2025-03-05 litellm-bedrock/claude-opus-4-8 msgs=3 in=3900 out=700 cw_5m=2400 cw_1h=0 cr=0 cost=0.024000
day ? litellm-bedrock/claude-opus-4-8 msgs=1 in=100 out=50 cw_5m=0 cw_1h=0 cr=0 cost=0.001050
"""


def _epoch(day: str) -> float:
    """Return the host-local noon of *day*, so the day bucket is the same in any zone."""
    return datetime.strptime(f"{day} 12:00", "%Y-%m-%d %H:%M").astimezone().timestamp()


def _rec(
    day: str | None,
    *,
    model: str | None = "bedrock/claude-opus-4-8",
    status: str = "success",
    usage: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """
    Build one log record in the shape the sidecar writes.

    ``day=None`` omits the timing block entirely, which is what puts a record in the
    ``?`` bucket rather than on a date.
    """
    response: dict[str, Any] = {"usage": usage if usage is not None else {}}
    if source is not None:
        response["_usage_source"] = source
    rec: dict[str, Any] = {"status": status, "response": response}
    if model is not None:
        rec["model"] = model
    if day is not None:
        rec["timing"] = {"start": _epoch(day), "end": _epoch(day) + 1}
    return rec


def _usage(  # noqa: PLR0913 -- one keyword per field of the usage block it builds
    fresh: int = 0,
    output_tokens: int = 0,
    *,
    cache_write: int = 0,
    cache_read: int = 0,
    nested_5m: int = 0,
    nested_1h: int = 0,
    top_level_5m: int = 0,
    input_tokens: int | None = None,
) -> dict[str, Any]:
    """
    Build a usage block in the shape the upstream APIs actually report.

    ``input_tokens`` is the *total* prompt size and includes the cached portion, so it
    is derived as ``fresh + cache_write + cache_read`` rather than given: the cost
    formula charges ``input_tokens - cache_write - cache_read`` at the input rate, and
    a fixture that treated the three as disjoint would price negative fresh input on
    every cached request. Measured across the tree: 42,910 of 43,736 cache-bearing
    requests have positive fresh input, 825 have exactly zero, and one has less than
    zero. ``input_tokens`` overrides the derivation, for pinning that last shape.

    ``nested_*`` is where the real tree puts a 5m/1h split and where the new schema
    reads it; ``top_level_5m`` is the ``usage.cache_creation`` path ``Bucket.add``
    reads and that nothing in the real tree writes. Both are exercised, so the golden
    pins what today's fold does with each.
    """
    usage: dict[str, Any] = {
        "input_tokens": (
            fresh + cache_write + cache_read if input_tokens is None else input_tokens
        ),
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }
    if nested_5m or nested_1h:
        usage["prompt_tokens_details"] = {
            "cache_creation_token_details": {
                "ephemeral_5m_input_tokens": nested_5m,
                "ephemeral_1h_input_tokens": nested_1h,
            }
        }
    if top_level_5m:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": top_level_5m}
    return usage


def _row_name(row: ProjectRow) -> str:
    """Return a row's project name; model display rows omit it and sort first."""
    return row.get("name", "")


def _serialize(report: StatsReport) -> str:
    """
    Render a report as sorted, exact text -- the form the golden is stored in.

    Sorted rather than in the fold's own order so the comparison is over the numbers
    rather than over dict insertion order, and costs at six decimal places so a
    rounding change cannot hide inside a displayed two-decimal figure.
    """

    def cells(bucket: Bucket) -> str:
        return (
            f"msgs={bucket.msgs} in={bucket.in_} out={bucket.out} "
            f"cw_5m={bucket.cw_5m} cw_1h={bucket.cw_1h} cr={bucket.cr} "
            f"cost={bucket.cost:.6f}"
        )

    lines = [f"unrecorded={report.unrecorded}"]
    lines += [
        f"row {_row_name(row)} sessions={row['sessions']} {cells(row['total'])} "
        f"unknown={row['total'].cost_unknown} unrecorded={row['total'].unrecorded}"
        for row in sorted(report.rows, key=_row_name)
    ]
    if report.orphaned is not None:
        total = report.orphaned["total"]
        lines.append(
            f"orphaned sessions={report.orphaned['sessions']} {cells(total)} "
            f"unknown={total.cost_unknown} unrecorded={total.unrecorded}"
        )
    lines += [
        f"model {model} {cells(bucket)}" for model, bucket in sorted(report.totals_by_model.items())
    ]
    lines += [
        f"day {day} {model} {cells(bucket)}"
        for day, by_model in sorted(report.totals_by_day_by_model.items())
        for model, bucket in sorted(by_model.items())
    ]
    return "\n".join(lines) + "\n"


@pytest.fixture
def stats_svc(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    make_fake_provider: Callable[..., FakeProvider],
    make_stats_service: Callable[[PricingService], StatsService],
) -> StatsService:
    """Return a StatsService priced from one fixed rate table, so costs are exact."""
    provider_service = mocker.Mock(spec=ProviderService)
    provider_service.get_provider.return_value = make_fake_provider(
        flat={"claude-opus-4-8": _RATES}
    )
    pricing = PricingService(provider_service=provider_service, display_service=display_mock)
    return make_stats_service(pricing)


@pytest.fixture
def golden_tree(
    tmp_path: Path,
    write_session: Callable[..., Path],
    link_project: Callable[[Path], None],
    index_logs: Callable[[], None],
) -> list[Path]:
    """
    Write the fixed log tree the golden was captured from, index it, return the registry.

    Two registered projects and one orphaned hash directory with no owner. Project
    ``alpha`` has run two providers, which is the case that makes a project's row the
    sum of several session subtrees.
    """
    alpha = tmp_path / "projects" / "alpha"
    beta = tmp_path / "projects" / "beta"
    orphan = tmp_path / "projects" / "deleted"
    # Created before anything is hashed: project_path_hash resolves first, so a project
    # dir that does not exist yet hashes to a different value than it will later.
    for project in (alpha, beta, orphan):
        project.mkdir(parents=True)

    write_session(
        alpha,
        "sess-a1",
        [
            # A plain priced request.
            _rec(_DAY_ONE, usage=_usage(1000, 250)),
            # Cache write and read, with a genuine split one level down. Today's fold
            # cannot see that split and charges all 3200 tokens at the 5m rate.
            _rec(
                _DAY_ONE,
                usage=_usage(
                    800, 500, cache_write=3200, cache_read=1500, nested_5m=1200, nested_1h=2000
                ),
            ),
            # The one shape that does populate usage.cache_creation. Nothing in the real
            # tree writes it; pinned so the replacement cannot quietly stop reading it.
            _rec(_DAY_ONE, usage=_usage(1200, 300, cache_write=1200, top_level_5m=1200)),
            # Usage recovered from the standard logging object rather than the response.
            _rec(_DAY_ONE, usage=_usage(), source="standard_logging_object"),
            # A successful request whose usage was never recorded: counts as a message,
            # contributes nothing, and is footnoted rather than hidden.
            _rec(_DAY_ONE, usage=_usage(), source="unrecoverable"),
            # A failure contributes nothing at all -- not even a message.
            _rec(_DAY_ONE, status="failure", usage=_usage(9999, 9999)),
            # No model: dropped silently, which the golden pins rather than endorses.
            _rec(_DAY_ONE, model=None, usage=_usage(4444, 4444)),
        ],
    )
    write_session(
        alpha,
        "sess-a2",
        [
            # No timing block, so this lands in the `?` day bucket.
            _rec(None, usage=_usage(100, 50)),
        ],
    )
    write_session(
        alpha,
        "sess-a3",
        [
            # A second provider under the same project. The display model carries the
            # provider, so this is a separate model row folding into one project row.
            _rec(_DAY_ONE, usage=_usage(500, 100)),
        ],
        provider="litellm-deepseek",
    )
    write_session(
        beta,
        "sess-b1",
        [
            _rec(_DAY_TWO, usage=_usage(1000, 300)),
            _rec(_DAY_TWO, usage=_usage(700, 300, cache_write=1200)),
            # The one-in-45,403 shape: a prompt total smaller than the cached portion it
            # is supposed to contain, which prices fresh input *negatively*. Pinned
            # because it exists in the real tree, and a replacement that clamped it at
            # zero would be a change in reported spend rather than a tidy-up.
            _rec(_DAY_TWO, usage=_usage(output_tokens=100, cache_write=1200, input_tokens=1000)),
        ],
    )
    # An orphaned hash: a log directory whose project is not in the registry. Given real
    # spend and an unrecorded request of its own, so the merged orphan row is not all
    # zeroes and the footnote counts across both owned and orphaned sessions.
    write_session(
        orphan,
        "sess-o1",
        [
            _rec(_DAY_ONE, usage=_usage(400, 80)),
            _rec(_DAY_ONE, usage=_usage(), source="unrecoverable"),
        ],
    )

    for project in (alpha, beta):
        link_project(project)
    index_logs()
    return [alpha, beta]


def test_the_aggregation_matches_the_pinned_file_scan_output(
    stats_svc: StatsService, golden_tree: list[Path]
) -> None:
    """
    The whole point of this module: the numbers, pinned, before the scan is deleted.

    A failure here is not a test to update. It means the aggregation changed, and what
    needs justifying is the change -- every figure below was produced by the
    implementation this one replaces.
    """
    report = stats_svc.build_report(golden_tree, UsageArgs(from_iso=None, until_iso=None))

    assert _serialize(report) == GOLDEN_REPORT


def test_a_pattern_narrows_the_report_to_one_project(
    stats_svc: StatsService, golden_tree: list[Path]
) -> None:
    """
    ``--pattern`` filters rows *and* the shared totals, and suppresses the orphan row.

    Pinned separately because the totals are recomputed over the selection rather than
    sliced out of the unfiltered ones -- so a replacement that filtered only the rows
    would still pass the test above.
    """
    report = stats_svc.build_report(
        golden_tree, UsageArgs(from_iso=None, until_iso=None, pattern=re.compile("beta"))
    )

    assert [row["name"] for row in report.rows] == ["beta"]
    assert report.orphaned is None
    assert sorted(report.totals_by_day_by_model) == [_DAY_TWO]
    assert report.totals_by_model["litellm-bedrock/claude-opus-4-8"].in_ == 3900


def test_a_window_bound_excludes_the_days_outside_it(
    stats_svc: StatsService, golden_tree: list[Path]
) -> None:
    """
    Day filtering happens per record during the fold, not over finished buckets.

    The ``?`` bucket is the case worth pinning: a record with no timing has no day to
    compare, and is excluded by any bound rather than treated as always in range.
    """
    report = stats_svc.build_report(golden_tree, UsageArgs(from_iso=_DAY_TWO, until_iso=_DAY_TWO))

    assert sorted(report.totals_by_day_by_model) == [_DAY_TWO]
    assert [row["name"] for row in report.rows] == ["beta"]
