# This file has been edited with the assistance of an AI tool.
"""
Unit tests for UsageTracker — daily usage tracking and usage.json writing.

What is under test is the day boundary, the payload, and the write-versus-touch decision.

Records are written into the central log tree and indexed, so a total here has come
through the parser and the schema rather than from a stubbed aggregate -- the arithmetic
that reaches the statusline is the arithmetic under test.
"""

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.constants import DAY_START_HOURS, LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.constants import MESSAGES_FILENAME
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.logs.usage_tracker import UsageTracker
from agent_wrap.domain.pricing.models import Bucket, TokenUsage
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.lib.daytime import get_day

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository
    from agent_wrap.infrastructure.logs.repositories.usage import UsageRepository


@pytest.fixture
def pricing() -> Mock:
    """Return a PricingService mock wired for the stats fold."""
    mock = Mock(spec=PricingService)
    mock.new_bucket.side_effect = Bucket
    mock.merged_bucket.side_effect = Bucket.merged
    mock.bucket_from_usage.side_effect = _bucket_from_usage
    # Pure factories -- they read no instance state, so the real implementations run.
    mock.usage_from_counts.side_effect = partial(PricingService.usage_from_counts, mock)
    mock.usage_from_bucket.side_effect = partial(PricingService.usage_from_bucket, mock)
    mock.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
    mock.compute_cost.return_value = 0.001
    mock.request_cache_ttl.return_value = None
    return mock


def _bucket_from_usage(usage: TokenUsage, *, msgs: int, unrecorded: int = 0) -> Bucket:
    """Stand in for ``PricingService.bucket_from_usage`` on the mocked pricing service."""
    bucket = Bucket()
    bucket.add(usage, 0.0)
    bucket.msgs = msgs
    bucket.unrecorded = unrecorded
    return bucket


@pytest.fixture
def stats_service(
    pricing: Mock,
    usage_repository: UsageRepository,
    log_ingest_repository: LogIngestRepository,
) -> StatsService:
    """Return a StatsService over the test's own logs database."""
    return StatsService(
        pricing_service=pricing,
        config_service=Mock(spec=ConfigService),
        usage_repository=usage_repository,
        log_ingest_repository=log_ingest_repository,
    )


@pytest.fixture
def clock(mocker: MockerFixture) -> dict[str, str]:
    """
    Return a one-entry dict holding what the tracker believes today is.

    A settable clock rather than a frozen one: the day boundary is the tracker's only
    remaining state, and the way to exercise it is to move the clock forward between two
    flushes — which is what actually happens at midnight. Defaults to the real current
    day, so every other test reads normally.
    """
    day = {"key": get_day(datetime.now(UTC), DAY_START_HOURS).isoformat()}
    mocker.patch.object(UsageTracker, "_current_day_key", staticmethod(lambda: day["key"]))
    return day


@pytest.fixture
def tracker(stats_service: StatsService, tmp_path: Path, clock: dict[str, str]) -> UsageTracker:
    """Return a UsageTracker whose _output_path lands under tmp_path."""
    assert clock  # constructed after the clock is in place, so it adopts that day
    tracker = UsageTracker(stats_service)
    tracker._output_path = tmp_path / "usage.json"
    return tracker


@pytest.fixture
def index_session(
    tmp_path: Path,
    log_ingest_repository: LogIngestRepository,
    log_session_repository: SessionRepository,
    log_request_repository: RequestRepository,
    display_mock: Mock,
) -> Callable[..., None]:
    """
    Return a factory that writes one session's records into the tree and indexes them.

    The provider and session default to a single session, because what these tests care
    about is the day a record falls on and the tokens it reports, not which project it
    came from — the tracker's totals are host-wide.
    """
    logs = LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
        config_service=Mock(spec=ConfigService),
        display_service=display_mock,
        log_ingest_repository=log_ingest_repository,
        log_session_repository=log_session_repository,
        log_request_repository=log_request_repository,
    )

    def _index(
        records: list[dict[str, Any]],
        *,
        provider: str = "litellm-bedrock",
        session: str = "sess-1",
    ) -> None:
        session_dir = tmp_path / LITELLM_LOGS_DIRNAME / "hashA" / provider / session
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / MESSAGES_FILENAME).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        report = logs.ingest_tree()
        assert report is not None
        assert report.ok

    return _index


@pytest.fixture
def make_record() -> Callable[..., dict[str, Any]]:
    """Return a factory that builds a messages.jsonl record dict."""

    def _make(  # noqa: PLR0913
        *,
        status: str = "success",
        model: str = "claude-sonnet-4-5",
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_read: int = 0,
        cache_creation: int = 0,
        start_ts: float | None = None,
    ) -> dict[str, Any]:
        ts = start_ts if start_ts is not None else datetime.now(UTC).timestamp()
        return {
            "status": status,
            "model": model,
            "timing": {"start": ts},
            "response": {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
            },
        }

    return _make


def _payload(tmp_path: Path) -> dict[str, Any]:
    """Read back the usage.json the tracker wrote."""
    return json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))


def test_flush_writes_all_zero_usage_json(tracker: UsageTracker, tmp_path: Path) -> None:
    """Flush against an empty index writes a zeroed-out usage.json."""
    tracker.flush()

    data = _payload(tmp_path)
    assert data == {
        "in": 0,
        "out": 0,
        "cache": 0,
        "cache_creation": 0,
        "cost": "$0.00",
        "requests": 0,
    }


def test_todays_records_are_counted(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    now_ts = datetime.now(UTC).timestamp()
    index_session(
        [
            make_record(input_tokens=200, output_tokens=100, cache_read=30, start_ts=now_ts),
            make_record(input_tokens=300, output_tokens=150, start_ts=now_ts),
        ]
    )

    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["out"], data["cache"], data["requests"]) == (500, 250, 30, 2)


def test_yesterdays_records_are_excluded(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """
    ``usage.json`` says "today", so the window is one day and the index applies it.

    Indexed and then excluded, rather than never read: the whole history is in the
    database, and it is the day bound on the aggregate that keeps yesterday out.
    """
    yesterday_ts = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    index_session([make_record(input_tokens=999, start_ts=yesterday_ts)])

    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["requests"]) == (0, 0)


def test_unknown_cost_model_produces_question_mark(
    pricing: Mock,
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """When compute_cost returns None, the cost field is '?'."""
    pricing.compute_cost.return_value = None
    index_session([make_record(input_tokens=100, output_tokens=50)])

    tracker.flush()

    assert _payload(tmp_path)["cost"] == "?"


def test_an_empty_index_does_not_crash(tracker: UsageTracker, tmp_path: Path) -> None:
    """The state of a host whose logs have never been indexed."""
    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["requests"]) == (0, 0)


def test_sessions_across_providers_are_aggregated(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """One host-wide total, so two sidecars' sessions sum rather than compete."""
    for i, provider in enumerate(["litellm-bedrock", "litellm-dashscope"]):
        index_session(
            [make_record(input_tokens=100, output_tokens=50)],
            provider=provider,
            session=f"sess-{i}",
        )

    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["out"], data["requests"]) == (200, 100, 2)


def test_an_appended_record_is_picked_up_on_the_next_flush(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """
    Each flush re-aggregates, so the totals follow the index without per-file state.

    The old tracker had to be told which file changed and replace that file's stored
    contribution. There is nothing to replace now, and no way for a stale contribution
    to survive a flush.
    """
    index_session([make_record(input_tokens=100, output_tokens=50)])
    tracker.flush()
    assert _payload(tmp_path)["in"] == 100

    index_session([make_record(input_tokens=1, output_tokens=1)])
    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["requests"]) == (101, 2)


def test_cache_creation_tokens_are_tracked(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    index_session(
        [make_record(input_tokens=200, output_tokens=100, cache_read=15, cache_creation=40)]
    )

    tracker.flush()

    data = _payload(tmp_path)
    assert (data["cache"], data["cache_creation"]) == (15, 40)


def test_detect_rollover_after_the_clock_crosses_the_boundary(
    tracker: UsageTracker, clock: dict[str, str]
) -> None:
    """detect_rollover returns True once the current day has moved past the tracked one."""
    clock["key"] = "2999-01-01"
    assert tracker.detect_rollover() is True


def test_detect_rollover_false_when_day_unchanged(tracker: UsageTracker) -> None:
    assert tracker.detect_rollover() is False


def test_a_rollover_moves_the_window_rather_than_clearing_state(
    stats_service: StatsService,
    clock: dict[str, str],
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """
    Flush detects a stale day itself, and then reports the new day rather than nothing.

    There is no state to clear any more: yesterday's requests stay indexed and stay
    correct, and the rollover only changes which day the aggregate asks for. So the
    tracker is set up mid-yesterday and rolled forward, and yesterday's spend drops out
    because it is yesterday's — not because a bucket was emptied.
    """
    yesterday = datetime.now(UTC) - timedelta(hours=48)
    clock["key"] = get_day(yesterday, DAY_START_HOURS).isoformat()
    tracker = UsageTracker(stats_service)
    tracker._output_path = tmp_path / "usage.json"
    index_session([make_record(input_tokens=100, output_tokens=50, start_ts=yesterday.timestamp())])
    tracker.flush()
    assert _payload(tmp_path)["in"] == 100

    clock["key"] = get_day(datetime.now(UTC), DAY_START_HOURS).isoformat()
    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["requests"]) == (0, 0)


def test_reset_adopts_the_current_day(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """Reset is now only about the day key, and today's totals survive it."""
    index_session([make_record(input_tokens=100, output_tokens=50)])
    tracker._today_key = "2000-01-01"

    tracker.reset()
    tracker.flush()

    assert tracker.detect_rollover() is False
    assert _payload(tmp_path)["in"] == 100


def test_flush_touches_when_payload_unchanged(
    tracker: UsageTracker, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A second flush with the same payload touches the file without rewriting it."""
    output = tmp_path / "usage.json"

    # Initial write.
    tracker.flush()
    assert output.is_file()
    original_mtime = output.stat().st_mtime
    original_content = output.read_text()

    write_spy = mocker.patch(
        "agent_wrap.domain.logs.usage_tracker.atomic_write_json", autospec=True
    )
    tracker.flush()

    write_spy.assert_not_called()
    assert output.stat().st_mtime >= original_mtime
    assert output.read_text() == original_content


def test_flush_creates_file_if_missing(tracker: UsageTracker, tmp_path: Path) -> None:
    """Flush on a missing file does a full write so the file is valid JSON."""
    output = tmp_path / "usage.json"

    tracker.flush()
    output.unlink()  # payload matches _last_output, but the file is gone

    tracker.flush()
    assert output.is_file()
    assert output.stat().st_size > 0  # not an empty file — valid JSON
    assert _payload(tmp_path)["in"] == 0


def test_flush_rewrites_payload_left_by_a_previous_run(
    tracker: UsageTracker, tmp_path: Path
) -> None:
    """A payload written by an earlier run is replaced, not touched, on the first flush."""
    output = tmp_path / "usage.json"
    output.write_text(
        json.dumps(
            {
                "in": 999,
                "out": 888,
                "cache": 0,
                "cache_creation": 0,
                "cost": "$9.99",
                "requests": 7,
            }
        ),
        encoding="utf-8",
    )

    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["out"], data["requests"]) == (0, 0, 0)
    assert data["cost"] == "$0.00"


def test_flush_rewrites_after_rollover_with_no_activity(
    stats_service: StatsService,
    clock: dict[str, str],
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    index_session: Callable[..., None],
) -> None:
    """A rollover with nothing indexed since zeroes the payload instead of touching it."""
    yesterday = datetime.now(UTC) - timedelta(hours=48)
    clock["key"] = get_day(yesterday, DAY_START_HOURS).isoformat()
    tracker = UsageTracker(stats_service)
    tracker._output_path = tmp_path / "usage.json"
    index_session([make_record(input_tokens=100, output_tokens=50, start_ts=yesterday.timestamp())])
    tracker.flush()
    assert _payload(tmp_path)["in"] == 100

    clock["key"] = get_day(datetime.now(UTC), DAY_START_HOURS).isoformat()
    tracker.flush()

    data = _payload(tmp_path)
    assert (data["in"], data["requests"]) == (0, 0)
