# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for scan orchestration and plan_pool heuristic."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

import agent_wrap.domain.stats.scan as scan_mod
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.scan import (
    accumulate_record,
    plan_pool,
    scan_logs_dir,
    scan_session_file,
)
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest_mock

    from agent_wrap.conftest import FakeProvider

_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}


@pytest.fixture
def pricing_service(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    make_fake_provider: Callable[..., FakeProvider],
) -> PricingService:
    """Return a priced PricingService."""
    fake = make_fake_provider(flat={"claude-opus-4-8": _RATES})
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value = fake
    return PricingService(provider_service=mock_ps, display_service=display_mock)


@pytest.fixture
def stats_svc(pricing_service: PricingService) -> StatsService:
    """Return a StatsService with the priced pricing_service."""
    return StatsService(pricing_service, config_service=Mock(spec=ConfigService))


def _day_epoch(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").astimezone().timestamp()


def _dated_rec(day: str, model: str = "claude-opus-4-8") -> dict[str, Any]:
    return {
        "status": "success",
        "model": model,
        "timing": {"start": _day_epoch(day)},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }


def _write_central_log(
    tool_dir: Path,
    hash_name: str,
    session_id: str,
    records: list[dict[str, Any]],
) -> Path:
    sdir = tool_dir / "litellm-logs" / hash_name / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return tool_dir / "litellm-logs" / hash_name


def _seed_many_dirs(tool_dir: Path, n: int, records_per: int) -> list[Path]:
    dirs = []
    for i in range(n):
        recs = [_dated_rec("2026-06-15") for _ in range(records_per)]
        dirs.append(_write_central_log(tool_dir, f"hash{i:04d}", "s1", recs))
    return dirs


def test_plan_pool_caps_workers_at_eight(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch.object(scan_mod.os, "cpu_count", return_value=64)
    workers, _chunksize = plan_pool(10_000)
    assert workers == 8


def test_plan_pool_scales_down_on_single_core(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch.object(scan_mod.os, "cpu_count", return_value=1)
    workers, _chunksize = plan_pool(10_000)
    assert workers == 1


def test_plan_pool_scales_workers_to_file_count(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch.object(scan_mod.os, "cpu_count", return_value=64)
    workers, _chunksize = plan_pool(20)
    assert workers == 2


@pytest.mark.parametrize(
    ("nfiles", "expected"),
    [
        (1, 1),
        (20, 2),
        (80, 4),
        (200, 6),
        (800, 8),
        (4430, 8),
        (50000, 8),
    ],
)
def test_plan_pool_chunksize_in_clamp_range(
    mocker: pytest_mock.MockFixture,
    nfiles: int,
    expected: int,
) -> None:
    mocker.patch.object(scan_mod.os, "cpu_count", return_value=20)
    _workers, chunksize = plan_pool(nfiles)
    assert chunksize == expected


def test_plan_pool_handles_unknown_cpu_count(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch.object(scan_mod.os, "cpu_count", return_value=None)
    workers, _chunksize = plan_pool(1000)
    assert workers == 1


def test_file_culling_skips_old_mtime(
    pricing_service: PricingService,
    tmp_path: Path,
) -> None:
    logs = tmp_path / ".claude" / "litellm-logs"
    sdir = logs / "litellm-bedrock" / "s1"
    sdir.mkdir(parents=True)
    msg = sdir / "messages.jsonl"
    msg.write_text(json.dumps(_dated_rec("2026-06-15")) + "\n", encoding="utf-8")
    old = _day_epoch("2026-01-01")
    os.utime(msg, (old, old))
    sessions, _last_ts, by_day, _by_source = scan_logs_dir(
        logs,
        pricing_service,
        from_iso="2026-06-01",
        until_iso="2026-06-30",
    )
    assert sessions == 0
    assert by_day == {}


def test_file_culling_keeps_recent_mtime_but_filters_records(
    pricing_service: PricingService,
    tmp_path: Path,
) -> None:
    logs = tmp_path / ".claude" / "litellm-logs"
    sdir = logs / "litellm-bedrock" / "s1"
    sdir.mkdir(parents=True)
    msg = sdir / "messages.jsonl"
    with msg.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_dated_rec("2026-06-15")) + "\n")
        f.write(json.dumps(_dated_rec("2026-07-15")) + "\n")
    sessions, _last_ts, by_day, _by_source = scan_logs_dir(
        logs,
        pricing_service,
        from_iso="2026-06-01",
        until_iso="2026-06-30",
    )
    assert sessions == 1
    assert set(by_day) == {"2026-06-15"}


def test_day_start_hours_shifts_record_bucket(
    pricing_service: PricingService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    # A record timestamped just after UTC midnight falls on the next UTC day
    # once the day-start offset is pushed forward past that instant.
    mocker.patch.object(scan_mod, "DAY_START_HOURS", 0)
    ts = datetime(2026, 6, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    logs = tmp_path / ".claude" / "litellm-logs"
    sdir = logs / "litellm-bedrock" / "s1"
    sdir.mkdir(parents=True)
    msg = sdir / "messages.jsonl"
    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": ts},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }
    msg.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    _sessions, _last_ts, by_day_0, _by_source = scan_logs_dir(logs, pricing_service)
    assert set(by_day_0) == {"2026-06-15"}

    mocker.patch.object(scan_mod, "DAY_START_HOURS", 2)
    _sessions, _last_ts, by_day_2, _by_source = scan_logs_dir(logs, pricing_service)
    assert set(by_day_2) == {"2026-06-14"}


def test_accumulate_record_carries_raw_timestamp() -> None:
    """The archive re-buckets by UTC hour, so the un-offset instant must survive."""
    ts = datetime(2026, 6, 15, 14, 37, 12, tzinfo=timezone.utc)
    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": ts.timestamp()},
        "response": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    }
    result = accumulate_record(rec, "litellm-bedrock", from_iso=None, until_iso=None)
    assert result.accumulated is True
    assert result.ts == ts


def test_accumulate_record_accumulates_timestampless_record_when_range_open() -> None:
    """An unwindowed scan keeps records the archive must file under the "?" key."""
    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "response": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    }
    result = accumulate_record(rec, "litellm-bedrock", from_iso=None, until_iso=None)
    assert result.accumulated is True
    assert result.ts is None
    assert result.day_key == "?"


@pytest.mark.parametrize(
    ("from_iso", "until_iso"),
    [("2026-06-01", None), (None, "2026-06-30"), ("2026-06-01", "2026-06-30")],
)
def test_accumulate_record_drops_timestampless_record_when_windowed(
    from_iso: str | None,
    until_iso: str | None,
) -> None:
    """A record with no timestamp cannot be range-checked, so any bound excludes it."""
    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "response": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    }
    result = accumulate_record(rec, "litellm-bedrock", from_iso=from_iso, until_iso=until_iso)
    assert result.accumulated is False


def test_scan_session_file_records_carry_timestamps(tmp_path: Path) -> None:
    """scan_session_file threads each record's ts through to its RawRecord."""
    ts = datetime(2026, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
    msg = tmp_path / "messages.jsonl"
    with msg.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "status": "success",
                    "model": "claude-opus-4-8",
                    "timing": {"start": ts.timestamp()},
                    "response": {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "status": "success",
                    "model": "claude-opus-4-8",
                    "response": {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                }
            )
            + "\n"
        )
    result = scan_session_file("litellm-bedrock", msg, from_iso=None, until_iso=None)
    assert [r.ts for r in result.records] == [ts, None]


def test_scan_log_dirs_serial_matches_scan_logs_dir(
    pricing_service: PricingService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
    subtests: pytest.Subtests,
) -> None:
    dirs = _seed_many_dirs(tmp_path / "tool", 3, records_per=2)
    mocker.patch("agent_wrap.domain.stats.service.SCAN_PARALLEL_MIN_FILES", 10**9)  # force serial
    cache = stats_svc.scan_log_dirs(dirs, from_iso=None, until_iso=None)
    for d in dirs:
        with subtests.test(msg=str(d)):
            expect = scan_logs_dir(d, pricing_service, from_iso=None, until_iso=None)
            got = cache[d]
            assert got[0] == expect[0]  # sessions
            assert {m: b.msgs for v in got[2].values() for m, b in v.items()} == {
                m: b.msgs for v in expect[2].values() for m, b in v.items()
            }


def test_scan_log_dirs_parallel_matches_serial(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
) -> None:
    dirs = _seed_many_dirs(tmp_path / "tool", 80, records_per=2)
    mocker.patch("agent_wrap.domain.stats.service.SCAN_PARALLEL_MIN_FILES", 10**9)
    serial = stats_svc.scan_log_dirs(dirs, from_iso=None, until_iso=None)
    mocker.patch("agent_wrap.domain.stats.service.SCAN_PARALLEL_MIN_FILES", 1)
    parallel = stats_svc.scan_log_dirs(dirs, from_iso=None, until_iso=None)

    def norm(cache: dict[Any, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for d, (sess, ts, by_day, _by_src) in cache.items():
            out[str(d)] = (
                sess,
                ts,
                {
                    day: {
                        m: (b.msgs, b.in_, b.out, b.cw, b.cr, round(b.cost, 9))
                        for m, b in v.items()
                    }
                    for day, v in by_day.items()
                },
            )
        return out

    assert norm(serial) == norm(parallel)


def test_scan_log_dirs_parallel_totals_match_records(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
) -> None:
    dirs = _seed_many_dirs(tmp_path / "tool", 80, records_per=3)
    mocker.patch("agent_wrap.domain.stats.service.SCAN_PARALLEL_MIN_FILES", 1)  # force parallel
    cache = stats_svc.scan_log_dirs(dirs, from_iso=None, until_iso=None)
    total_msgs = sum(
        b.msgs for _, _, by_day, _ in cache.values() for v in by_day.values() for b in v.values()
    )
    assert total_msgs == 80 * 3
