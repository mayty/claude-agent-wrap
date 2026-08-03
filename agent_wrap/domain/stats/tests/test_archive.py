# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for the orphaned-usage archive's read/write/merge helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.archive import (
    archive_time_keys,
    fold_records_into_archive,
    merge_archives,
    read_archive,
    write_archive,
)
from agent_wrap.domain.stats.models import RawRecord

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import Mock

    import pytest_mock

    from agent_wrap.conftest import FakeProvider
    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.stats.models import ArchiveDoc, ArchiveLeaf

_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}


@pytest.fixture
def pricing_service(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    make_fake_provider: Callable[..., FakeProvider],
) -> PricingService:
    """Return a PricingService that prices claude-opus-4-8."""
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value = make_fake_provider(flat={"claude-opus-4-8": _RATES})
    return PricingService(provider_service=mock_ps, display_service=display_mock)


def _usage(  # noqa: PLR0913
    *,
    in_tokens: int = 0,
    out_tokens: int = 0,
    cw_flat: int = 0,
    cw_5m: int = 0,
    cw_1h: int = 0,
    cr: int = 0,
) -> TokenUsage:
    cache_creation: dict[str, int] = {}
    if cw_5m:
        cache_creation["ephemeral_5m_input_tokens"] = cw_5m
    if cw_1h:
        cache_creation["ephemeral_1h_input_tokens"] = cw_1h
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_creation_input_tokens": cw_flat,
        "cache_read_input_tokens": cr,
        "cache_creation": cache_creation,
    }


def _record(
    ts: datetime | None,
    *,
    model: str = "litellm-bedrock/claude-opus-4-8",
    source: str = "native",
    usage: TokenUsage | None = None,
    unrecorded: bool = False,
) -> RawRecord:
    return RawRecord(
        day_key="unused",
        display_model=model,
        usage=usage if usage is not None else _usage(in_tokens=10, out_tokens=5),
        source=source,
        unrecorded=unrecorded,
        ts=ts,
    )


def _leaf(doc: ArchiveDoc, date: str, hour: str, model: str, source: str) -> ArchiveLeaf:
    return doc[date][hour][model][source]


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (datetime(2026, 7, 20, 14, 37, tzinfo=timezone.utc), ("2026-07-20", "14")),
        (datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc), ("2026-01-02", "00")),
        (datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc), ("2026-01-02", "23")),
        (None, ("?", "?")),
    ],
)
def test_archive_time_keys(ts: datetime | None, expected: tuple[str, str]) -> None:
    assert archive_time_keys(ts) == expected


def test_archive_time_keys_converts_to_utc() -> None:
    """A non-UTC timestamp is normalized, so the hour key is offset-independent."""
    aware = datetime(2026, 7, 20, 2, 30, tzinfo=timezone(timedelta(hours=5)))
    assert archive_time_keys(aware) == ("2026-07-19", "21")


def test_fold_sums_same_cell_across_records(pricing_service: PricingService) -> None:
    ts = datetime(2026, 7, 20, 14, 5, tzinfo=timezone.utc)
    doc = fold_records_into_archive(
        [_record(ts), _record(ts.replace(minute=55)), _record(ts)], pricing_service
    )
    leaf = _leaf(doc, "2026-07-20", "14", "litellm-bedrock/claude-opus-4-8", "native")
    assert leaf["msgs"] == 3
    assert leaf["input_tokens"] == 30
    assert leaf["output_tokens"] == 15


def test_fold_splits_by_hour_and_date(pricing_service: PricingService) -> None:
    doc = fold_records_into_archive(
        [
            _record(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)),
            _record(datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)),
            _record(datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)),
        ],
        pricing_service,
    )
    assert set(doc) == {"2026-07-20", "2026-07-21"}
    assert set(doc["2026-07-20"]) == {"14", "15"}


def test_fold_splits_by_model_and_source(pricing_service: PricingService) -> None:
    ts = datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)
    doc = fold_records_into_archive(
        [
            _record(ts),
            _record(ts, source="unrecoverable", unrecorded=True),
            _record(ts, model="litellm-bedrock/claude-sonnet-4-5"),
        ],
        pricing_service,
    )
    hour = doc["2026-07-20"]["14"]
    assert set(hour) == {"litellm-bedrock/claude-opus-4-8", "litellm-bedrock/claude-sonnet-4-5"}
    assert set(hour["litellm-bedrock/claude-opus-4-8"]) == {"native", "unrecoverable"}
    assert hour["litellm-bedrock/claude-opus-4-8"]["unrecoverable"]["unrecorded"] == 1


def test_fold_files_timestampless_records_under_unknown_key(
    pricing_service: PricingService,
) -> None:
    doc = fold_records_into_archive([_record(None)], pricing_service)
    assert _leaf(doc, "?", "?", "litellm-bedrock/claude-opus-4-8", "native")["msgs"] == 1


def test_fold_normalizes_model_names(pricing_service: PricingService) -> None:
    """Date-stamped model ids collapse onto their base name, as live scans do."""
    doc = fold_records_into_archive(
        [
            _record(
                datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
                model="litellm-bedrock/claude-opus-4-8-20260515",
            ),
            _record(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)),
        ],
        pricing_service,
    )
    hour = doc["2026-07-20"]["14"]
    assert set(hour) == {"litellm-bedrock/claude-opus-4-8"}
    assert hour["litellm-bedrock/claude-opus-4-8"]["native"]["msgs"] == 2


def test_fold_preserves_explicit_cache_tier_split(pricing_service: PricingService) -> None:
    doc = fold_records_into_archive(
        [
            _record(
                datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
                usage=_usage(cw_flat=300, cw_5m=100, cw_1h=200),
            )
        ],
        pricing_service,
    )
    leaf = _leaf(doc, "2026-07-20", "14", "litellm-bedrock/claude-opus-4-8", "native")
    assert leaf["cache_write_5m"] == 100
    assert leaf["cache_write_1h"] == 200


def test_fold_applies_flat_cache_write_fallback(pricing_service: PricingService) -> None:
    """With no ephemeral split, Bucket.add charges the flat total at the 5m rate."""
    doc = fold_records_into_archive(
        [_record(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc), usage=_usage(cw_flat=500))],
        pricing_service,
    )
    leaf = _leaf(doc, "2026-07-20", "14", "litellm-bedrock/claude-opus-4-8", "native")
    assert leaf["cache_write_5m"] == 500
    assert leaf["cache_write_1h"] == 0


def test_fold_stores_no_cost(pricing_service: PricingService) -> None:
    """Cost must be re-derived at read time, so it is never persisted."""
    doc = fold_records_into_archive(
        [_record(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))], pricing_service
    )
    leaf = _leaf(doc, "2026-07-20", "14", "litellm-bedrock/claude-opus-4-8", "native")
    assert "cost" not in leaf


def test_fold_empty_records_yields_empty_doc(pricing_service: PricingService) -> None:
    assert fold_records_into_archive([], pricing_service) == {}


def test_merge_sums_overlapping_leaves() -> None:
    dst: ArchiveDoc = {"2026-07-20": {"14": {"p/m": {"native": _archived(msgs=2, in_tokens=100)}}}}
    merge_archives(
        dst, {"2026-07-20": {"14": {"p/m": {"native": _archived(msgs=3, in_tokens=50)}}}}
    )
    assert dst["2026-07-20"]["14"]["p/m"]["native"]["msgs"] == 5
    assert dst["2026-07-20"]["14"]["p/m"]["native"]["input_tokens"] == 150


def test_merge_adds_disjoint_branches() -> None:
    dst: ArchiveDoc = {"2026-07-20": {"14": {"p/m": {"native": _archived(msgs=1)}}}}
    merge_archives(
        dst,
        {
            "2026-07-20": {
                "15": {"p/m": {"native": _archived(msgs=1)}},
                "14": {"p/other": {"native": _archived(msgs=1)}},
            },
            "2026-07-21": {"14": {"p/m": {"native": _archived(msgs=1)}}},
        },
    )
    assert set(dst) == {"2026-07-20", "2026-07-21"}
    assert set(dst["2026-07-20"]) == {"14", "15"}
    assert set(dst["2026-07-20"]["14"]) == {"p/m", "p/other"}


def test_merge_does_not_alias_source_leaves() -> None:
    """A leaf copied into an empty branch must not be mutated by later merges."""
    src: ArchiveDoc = {"2026-07-20": {"14": {"p/m": {"native": _archived(msgs=1)}}}}
    dst: ArchiveDoc = {}
    merge_archives(dst, src)
    merge_archives(dst, src)
    assert dst["2026-07-20"]["14"]["p/m"]["native"]["msgs"] == 2
    assert src["2026-07-20"]["14"]["p/m"]["native"]["msgs"] == 1


def test_merge_tolerates_leaf_missing_fields() -> None:
    """A hand-edited archive missing a key merges instead of raising."""
    dst: ArchiveDoc = {"2026-07-20": {"14": {"p/m": {"native": {"msgs": 1}}}}}  # pyrefly: ignore [bad-typed-dict-key]
    merge_archives(dst, {"2026-07-20": {"14": {"p/m": {"native": {"msgs": 2}}}}})  # pyrefly: ignore [bad-typed-dict-key]
    assert dst["2026-07-20"]["14"]["p/m"]["native"]["msgs"] == 3


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    doc: ArchiveDoc = {"2026-07-20": {"14": {"p/m": {"native": _archived(msgs=2, in_tokens=7)}}}}
    path = tmp_path / "archive.json"
    write_archive(path, doc)
    assert read_archive(path) == doc


def test_write_sorts_chronologically(tmp_path: Path) -> None:
    path = tmp_path / "archive.json"
    write_archive(
        path,
        {
            "2026-07-21": {"09": {"p/m": {"native": _archived()}}},
            "2026-07-20": {
                "15": {"p/b": {"native": _archived()}},
                "09": {"p/z": {"native": _archived()}, "p/a": {"native": _archived()}},
            },
        },
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == ["2026-07-20", "2026-07-21"]
    assert list(data["2026-07-20"]) == ["09", "15"]
    assert list(data["2026-07-20"]["09"]) == ["p/a", "p/z"]


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "archive.json"
    write_archive(path, {"2026-07-20": {"14": {"p/m": {"native": _archived()}}}})
    assert path.is_file()


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_archive(tmp_path / "nope.json") == {}


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2, 3]", '"a string"', "null"])
def test_read_corrupt_file_returns_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "archive.json"
    path.write_text(content, encoding="utf-8")
    assert read_archive(path) == {}


def test_read_directory_returns_empty(tmp_path: Path) -> None:
    """An OSError on read is swallowed like any other unreadable-archive case."""
    path = tmp_path / "archive.json"
    path.mkdir()
    assert read_archive(path) == {}


def _archived(  # noqa: PLR0913
    *,
    msgs: int = 1,
    in_tokens: int = 0,
    out_tokens: int = 0,
    cw_5m: int = 0,
    cw_1h: int = 0,
    cr: int = 0,
    unrecorded: int = 0,
) -> ArchiveLeaf:
    return {
        "msgs": msgs,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_write_5m": cw_5m,
        "cache_write_1h": cw_1h,
        "cache_read": cr,
        "unrecorded": unrecorded,
    }
