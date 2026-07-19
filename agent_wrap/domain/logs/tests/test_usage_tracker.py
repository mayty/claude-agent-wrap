# This file has been edited with the assistance of an AI tool.
"""Unit tests for UsageTracker — daily usage tracking and usage.json writing."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.usage_tracker import UsageTracker
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> Mock:
    """Return a PricingService mock wired for fold_raw_to_buckets / price_buckets."""
    mock = Mock(spec=PricingService)
    mock.new_bucket.side_effect = Bucket
    mock.normalize_model.side_effect = lambda m: m
    mock.compute_cost.return_value = 0.001
    mock.request_cache_ttl.return_value = None
    return mock


@pytest.fixture
def stats_service(pricing: Mock) -> StatsService:
    """Return a StatsService backed by mock PricingService and ConfigService."""
    return StatsService(pricing, Mock(spec=ConfigService))


@pytest.fixture
def tracker(pricing: Mock, stats_service: StatsService, tmp_path: Path) -> UsageTracker:
    """Return a UsageTracker whose _output_path lands under tmp_path."""
    t = UsageTracker(pricing, stats_service)
    t._output_path = tmp_path / "usage.json"
    return t


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
        ts = start_ts if start_ts is not None else datetime.now(timezone.utc).timestamp()
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


@pytest.fixture
def write_messages_file() -> Callable[[Path, list[dict[str, Any]]], None]:
    """Return a factory that writes records as NDJSON to a file path."""

    def _write(path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    return _write


# ---------------------------------------------------------------------------
# empty tracker
# ---------------------------------------------------------------------------


def test_flush_writes_all_zero_usage_json(tracker: UsageTracker, tmp_path: Path) -> None:
    """Flush on an empty tracker writes a zeroed-out usage.json."""
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0
    assert data["out"] == 0
    assert data["cache"] == 0
    assert data["cache_creation"] == 0
    assert data["cost"] == "$0.00"
    assert data["requests"] == 0
    assert "updated_at" not in data


# ---------------------------------------------------------------------------
# single file
# ---------------------------------------------------------------------------


def test_todays_records_are_counted(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """A single file with today's records is scanned and its totals appear in output."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(
        mf,
        [
            make_record(input_tokens=200, output_tokens=100, cache_read=30, start_ts=now_ts),
            make_record(input_tokens=300, output_tokens=150, start_ts=now_ts),
        ],
    )

    changed = tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    assert changed is True
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 500
    assert data["out"] == 250
    assert data["cache"] == 30
    assert data["requests"] == 2


def test_yesterdays_records_are_excluded(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """Records from before today (per DAY_START_HOURS) are excluded."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    yesterday_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    write_messages_file(mf, [make_record(input_tokens=999, start_ts=yesterday_ts)])

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0
    assert data["requests"] == 0


def test_unknown_cost_model_produces_question_mark(
    pricing: Mock,
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """When compute_cost returns None, the cost field is '?'."""
    pricing.compute_cost.return_value = None

    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["cost"] == "?"


def test_empty_file_does_not_crash(tracker: UsageTracker, tmp_path: Path) -> None:
    """An empty messages.jsonl produces no error and contributes zero."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    mf.parent.mkdir(parents=True)
    mf.write_text("", encoding="utf-8")

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0
    assert data["requests"] == 0


# ---------------------------------------------------------------------------
# fingerprint / mtime
# ---------------------------------------------------------------------------


def test_unchanged_file_is_skipped(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """update_file returns False when the stat fingerprint hasn't changed."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])

    info = (mf.stat().st_mtime_ns, mf.stat().st_size)
    assert tracker.update_file(mf, info) is True  # first call — fingerprint stored
    assert tracker.update_file(mf, info) is False  # same stat — skipped


def test_file_with_yesterdays_mtime_is_skipped(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """A file whose mtime predates today's boundary is skipped without scanning."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=999, output_tokens=999, start_ts=now_ts)])
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    os.utime(mf, (yesterday.timestamp(), yesterday.timestamp()))

    info = (mf.stat().st_mtime_ns, mf.stat().st_size)
    assert tracker.update_file(mf, info) is False  # skipped — mtime from yesterday
    tracker.flush()
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0


def test_file_touched_today_is_scanned(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """After touching the file to today, it is scanned normally."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])
    now = datetime.now(timezone.utc)
    os.utime(mf, (now.timestamp(), now.timestamp()))

    info = (mf.stat().st_mtime_ns, mf.stat().st_size)
    assert tracker.update_file(mf, info) is True  # scanned — mtime is today
    tracker.flush()
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 100


# ---------------------------------------------------------------------------
# multiple files
# ---------------------------------------------------------------------------


def test_multiple_files_are_aggregated(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """Contributions from multiple files are merged into one total."""
    now_ts = datetime.now(timezone.utc).timestamp()
    for i, provider in enumerate(["litellm-bedrock", "litellm-dashscope"]):
        mf = tmp_path / "litellm-logs" / provider / f"sess-{i}" / "messages.jsonl"
        write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])

    mf1 = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-0" / "messages.jsonl"
    mf2 = tmp_path / "litellm-logs" / "litellm-dashscope" / "sess-1" / "messages.jsonl"
    tracker.update_file(mf1, (mf1.stat().st_mtime_ns, mf1.stat().st_size))
    tracker.update_file(mf2, (mf2.stat().st_mtime_ns, mf2.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 200
    assert data["out"] == 100
    assert data["requests"] == 2


# ---------------------------------------------------------------------------
# removal
# ---------------------------------------------------------------------------


def test_removed_file_contribution_is_dropped(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """After remove_file, the file's contribution is gone."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=500, output_tokens=250, start_ts=now_ts)])

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 500

    tracker.remove_file(mf)
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0
    assert data["requests"] == 0


# ---------------------------------------------------------------------------
# rollover
# ---------------------------------------------------------------------------


def test_detect_rollover_after_manual_day_shift(tracker: UsageTracker) -> None:
    """detect_rollover returns True when _today_key is behind _current_day_key."""
    tracker._today_key = "2000-01-01"
    assert tracker.detect_rollover() is True


def test_detect_rollover_false_when_day_unchanged(tracker: UsageTracker) -> None:
    """detect_rollover returns False on the same day."""
    assert tracker.detect_rollover() is False


def test_flush_resets_on_rollover(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """Flush clears state when the day has changed since last update."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))

    # Force an old day key so flush detects rollover.
    tracker._today_key = "2000-01-01"
    tracker.flush()

    # After rollover-triggered reset, all file buckets are cleared.
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0
    assert data["requests"] == 0


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_clears_state(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """Reset clears all file buckets, fingerprints, and refreshes _today_key."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])

    info = (mf.stat().st_mtime_ns, mf.stat().st_size)
    tracker.update_file(mf, info)
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 100

    tracker.reset()
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 0
    assert data["requests"] == 0

    # Fingerprints cleared, so same stat triggers a re-scan.
    assert tracker.update_file(mf, info) is True


# ---------------------------------------------------------------------------
# cache creation
# ---------------------------------------------------------------------------


def test_cache_creation_tokens_are_tracked(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """Cache write tokens appear under cache_creation in output."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(
        mf,
        [
            make_record(
                input_tokens=200,
                output_tokens=100,
                cache_read=15,
                cache_creation=40,
                start_ts=now_ts,
            )
        ],
    )

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["cache"] == 15
    assert data["cache_creation"] == 40


# ---------------------------------------------------------------------------
# update replaces
# ---------------------------------------------------------------------------


def test_updating_same_file_replaces_old_contribution(
    tracker: UsageTracker,
    tmp_path: Path,
    make_record: Callable[..., dict[str, Any]],
    write_messages_file: Callable[[Path, list[dict[str, Any]]], None],
) -> None:
    """Calling update_file twice with different stat replaces the old contribution."""
    mf = tmp_path / "litellm-logs" / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    now_ts = datetime.now(timezone.utc).timestamp()
    write_messages_file(mf, [make_record(input_tokens=100, output_tokens=50, start_ts=now_ts)])

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 100

    # Append a record so stat changes, then update again.
    with mf.open("a", encoding="utf-8") as f:
        f.write(json.dumps(make_record(input_tokens=1, output_tokens=1, start_ts=now_ts)) + "\n")

    tracker.update_file(mf, (mf.stat().st_mtime_ns, mf.stat().st_size))
    tracker.flush()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["in"] == 101
    assert data["requests"] == 2


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


def test_flush_touch_when_content_unchanged(tracker: UsageTracker, tmp_path: Path) -> None:
    """flush(content_changed=False) touches the file without rewriting content."""
    output = tmp_path / "usage.json"

    # Initial write.
    tracker.flush(content_changed=True)
    assert output.is_file()
    original_mtime = output.stat().st_mtime
    original_content = output.read_text()

    # Touch-only: mtime changes but content is the same.
    tracker.flush(content_changed=False)
    assert output.stat().st_mtime >= original_mtime
    assert output.read_text() == original_content


def test_touch_creates_file_if_missing(tracker: UsageTracker, tmp_path: Path) -> None:
    """Flush on a missing file does a full write so the file is valid JSON."""
    output = tmp_path / "usage.json"

    assert not output.is_file()
    tracker.flush(content_changed=False)
    assert output.is_file()
    assert output.stat().st_size > 0  # not an empty file — valid JSON
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["in"] == 0
