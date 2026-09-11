# This file has been created with the assistance of an AI tool.
"""Data models for the stats domain."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, TypedDict

if TYPE_CHECKING:
    import re
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from agent_wrap.domain.pricing.models import Bucket


@dataclass
class UsageArgs:
    from_iso: str | None = None
    until_iso: str | None = None
    verbose: bool = False
    pattern: re.Pattern[str] | None = None
    refresh: bool = False


class Group:
    """Per-transient-project accumulator across one or more physical paths."""

    __slots__ = ("exists", "last_ts", "name", "root", "sessions", "total", "transient")

    def __init__(
        self,
        root: Path,
        name: str,
        *,
        transient: bool,
        new_bucket: Callable[[], Bucket],
    ) -> None:
        self.root = root
        self.name = name
        self.transient = transient
        self.total = new_bucket()
        self.sessions = 0
        self.last_ts: datetime | None = None
        self.exists = False


# One project hash's whole in-window usage, folded and priced.
#
# Keyed by hash rather than by project path because that is the only project identity
# the index carries; the caller resolves a hash to a registered project, or to none at
# all, which is what makes its spend orphaned. *sessions* counts the distinct sessions
# that contributed, and *last_ts* is the newest contributing request's exact instant.
class HashUsage(NamedTuple):
    sessions: int
    last_ts: datetime | None
    by_day: dict[str, dict[str, Bucket]]
    by_source: dict[str, dict[str, Bucket]]


# The folded usage of every project hash the index holds, so one aggregate serves the
# project rows, the shared totals and the orphaned row without being run three times.
type UsageCache = dict[str, HashUsage]


class _ProjectRowBase(TypedDict):
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
    sessions: int
    last_ts: datetime | None
    total: Bucket


# The pre-collapse bucket key carrying the UTC instant a bucket's usage falls into.
# Weekday uses ``datetime.weekday()`` (0=Monday ... 6=Sunday); hour is the UTC hour
# (0-23). Both are None for records with no timestamp, so providers with
# time-of-day pricing can charge the conservative peak rate when the instant is
# unknown.
class HourKey(NamedTuple):
    weekday: int | None
    hour: int | None


# Buckets keyed outer → hour → model, produced before pricing collapses the hour
# axis. The outer key is a stats day (by_day) or a usage source (by_source); the
# inner key is an :class:`HourKey` so both weekday and UTC hour survive to pricing.
HourBuckets = dict[str, dict[HourKey, dict[str, "Bucket"]]]


# Return type for delete_orphaned_logs.
#
# *removed*/*freed_bytes* count only dirs actually deleted, so they may fall
# short of the pre-confirmation estimate when a ``rmtree`` failed.
class CleanupResult(NamedTuple):
    removed: int
    freed_bytes: int


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


class CleanupScope(NamedTuple):
    """What a cleanup would remove, surveyed before anything is deleted."""

    orphaned_dirs: list[Path]
    stale_paths: list[Path]
    freed_estimate: int

    @property
    def is_empty(self) -> bool:
        return not self.orphaned_dirs and not self.stale_paths


class CleanupOutcome(NamedTuple):
    """
    What a cleanup actually did.

    *removed_paths* is the registry entries pruned, which is a separate list from the
    log directories in *result*: a project can be in the registry with its logs already
    gone, or have logs and still be registered.
    """

    result: CleanupResult
    removed_paths: list[Path]


class StatsReport(NamedTuple):
    """
    Everything ``agent stats`` renders for one selection window.

    ``rows`` holds only projects that contributed sessions; ``orphaned`` is the
    ``<orphaned>`` row -- the spend of every indexed project hash the registry does not
    claim -- or None when the pattern suppresses it. The totals already include that
    spend, so the tables agree with each other.
    ``unrecorded`` counts successful requests whose usage was never logged, which the
    caller footnotes because those requests contribute $0 to the costs above.
    """

    rows: list[ProjectRow]
    totals_by_model: dict[str, Bucket]
    totals_by_day_by_model: dict[str, dict[str, Bucket]]
    totals_by_source: dict[str, dict[str, Bucket]]
    orphaned: OrphanedResult | None
    unrecorded: int


@dataclass(frozen=True)
class WindowError:
    """
    A rejected ``--from``/``--until``/``--days`` combination, with the reason to print.

    Deliberately not a NamedTuple: it shares a return union with the resolved
    ``(from_iso, until_iso)`` pair, and a 1-tuple there would be structurally
    unpackable — the type checker could not tell the failure from the success.
    """

    message: str
