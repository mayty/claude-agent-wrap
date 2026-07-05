# This file has been created with the assistance of an AI tool.
"""Data models for the stats domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from agent_wrap.domain.pricing.models import Bucket, TokenUsage


@dataclass
class UsageArgs:
    registry_path: Path
    from_iso: str | None = None
    until_iso: str | None = None
    verbose: bool = False


class Group:
    """Per-transient-project accumulator across one or more physical paths."""

    __slots__ = ("exists", "last_ts", "name", "root", "sessions", "total", "transient")

    def __init__(
        self,
        root: Path,
        name: str,
        *,
        transient: bool,
        new_bucket: Callable[[], Any],
    ) -> None:
        self.root = root
        self.name = name
        self.transient = transient
        self.total = new_bucket()
        self.sessions = 0
        self.last_ts: datetime | None = None
        self.exists = False


# A single session file's contribution. The two dicts are plain (not
# defaultdict) so the result pickles cleanly back from a pool worker.
# *by_day* is {day: {model: Bucket}}, *by_source* is {source: {model: Bucket}}.
class FileResult(NamedTuple):
    had_record: bool
    last_ts: datetime | None
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]


# A unit of parallel work. *dir_index* tags which logs dir the file belongs to
# so the parent can fold results per dir (a project's sessions vs. an orphaned
# dir's) without the worker knowing.
class WorkUnit(NamedTuple):
    dir_index: int
    provider_name: str
    messages_file: Path


# A scan cache maps each logs dir to its folded
# (sessions, last_ts, by_day, by_source) result, so the
# aggregation pass can look results up instead of re-scanning.
class DirResult(NamedTuple):
    sessions: int
    last_ts: datetime | None
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]


ScanCache = dict["Path", DirResult]


# A raw record returned by scan workers. Workers produce these without any
# pricing-domain knowledge; the master normalizes model names and folds into
# Buckets.
class RawRecord(NamedTuple):
    day_key: str
    display_model: str
    usage: TokenUsage
    source: str
    unrecorded: bool


# A raw file result from a pool worker.
class RawFileResult(NamedTuple):
    had_record: bool
    last_ts: datetime | None
    records: list[RawRecord]


# Return type for accumulate_record: extracted fields from one log record.
class AccumulatedRecord(NamedTuple):
    accumulated: bool
    ts: datetime | None
    day_key: str | None
    display_model: str | None
    usage: TokenUsage | None
    source: str
    unrecorded: bool


# Return type for scan_project: a single project's scan result plus existence flag.
class ScanProjectResult(NamedTuple):
    sessions: int
    last_ts: datetime | None
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]
    exists: bool


class _ProjectRowBase(TypedDict):
    """Fields common to project rows and model display rows."""

    path: Path
    exists: bool
    sessions: int
    last_ts: datetime | None
    total: Bucket
    cost: float | None


class ProjectRow(_ProjectRowBase, total=False):
    """
    A project row for rendering the project tree.

    ``name`` and ``transient`` are optional — model display rows (produced by
    ``_model_display_rows``) omit them, and the tree renderer only accesses
    ``name`` when ``transient`` is truthy.
    """

    name: str
    transient: bool


class OrphanedResult(TypedDict):
    """Aggregated result for orphaned sessions."""

    sessions: int
    last_ts: datetime | None
    total: Bucket


# Return type for aggregate_projects: the four render inputs rolled up across all projects.
class AggregateResult(NamedTuple):
    rows: list[ProjectRow]
    totals_by_model: dict[str, Bucket]
    totals_by_day_by_model: dict[str, dict[str, Bucket]]
    totals_by_source: dict[str, dict[str, Bucket]]


# Return type for resolve_group: a project path resolved to its transient group.
class GroupResult(NamedTuple):
    group_root: Path
    display_name: str
    is_transient: bool
