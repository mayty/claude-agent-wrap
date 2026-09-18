# This file has been created with the assistance of an AI tool.
"""Token usage stats aggregation — domain service."""

import shutil
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    DAY_START_HOURS,
    LITELLM_LOGS_DIRNAME,
    ORPHANED_LABEL,
    TOOL_DIR,
)
from agent_wrap.domain.stats.constants import DEFAULT_DAYS, MARKER_NAME
from agent_wrap.domain.stats.fold import fold_cells
from agent_wrap.domain.stats.format_utils import hour_bounds
from agent_wrap.domain.stats.models import (
    AggregateResult,
    CleanupOutcome,
    CleanupResult,
    CleanupScope,
    Group,
    GroupResult,
    HashUsage,
    OrphanedResult,
    ProjectRow,
    StatsReport,
    UsageArgs,
    WindowError,
)
from agent_wrap.lib.daytime import get_day
from agent_wrap.lib.utils import directory_size

if TYPE_CHECKING:
    import re
    from pathlib import Path

    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.models import UsageCache
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.usage import UsageRepository


class StatsService:
    def __init__(
        self,
        pricing_service: PricingService,
        config_service: ConfigService,
        usage_repository: UsageRepository,
        log_ingest_repository: LogIngestRepository,
    ) -> None:
        self._pricing = pricing_service
        self._config = config_service
        self._usage = usage_repository
        self._ingest = log_ingest_repository

    def day_totals(self, day_key: str) -> Bucket:
        """
        Return one stats day's whole priced usage, across every project on the host.

        What ``usage.json`` holds. Costs the number of *cells* in one day -- a few dozen
        -- rather than the size of the files the day's requests were written to.
        """
        cache = self.usage_cache(from_iso=day_key, until_iso=day_key)
        return self._pricing.merged_bucket(
            bucket
            for usage in cache.values()
            for by_model in usage.by_day.values()
            for bucket in by_model.values()
        )

    def usage_cache(
        self,
        *,
        from_iso: str | None,
        until_iso: str | None,
        refresh_pricing_data: bool = False,
    ) -> UsageCache:
        """
        Read the index once and fold it into per-project-hash priced usage.

        One aggregate serves the project rows, the shared totals and the orphaned row, so
        the three can never disagree about a request. The window is applied in SQL as an
        hour-bucket range and again per cell as a day rule -- see :func:`hour_bounds`.
        """
        from_hour, until_hour = hour_bounds(from_iso, until_iso)
        cells = self._usage.usage_cells(from_hour=from_hour, until_hour=until_hour)
        return fold_cells(
            cells,
            self._pricing,
            from_iso=from_iso,
            until_iso=until_iso,
            refresh_pricing_data=refresh_pricing_data,
        )

    def aggregate_projects(
        self,
        projects: list[Path],
        cache: UsageCache,
        owners: dict[Path, str],
    ) -> AggregateResult:
        """
        Roll each project's folded usage up into the render inputs.

        A project absent from *owners* has no log directory, and a hash absent from
        *cache* contributed nothing in the window; either way it gets an empty
        :class:`HashUsage` rather than a special case.

        Takes no bounds: the window was applied when *cache* was read, so the project
        rows and the shared totals fold from the same cells.
        """
        groups: dict[Path, Group] = {}
        totals_by_model: dict[str, Bucket] = defaultdict(self._pricing.new_bucket)
        totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(
            lambda: defaultdict(self._pricing.new_bucket)
        )

        for path in projects:
            exists = path in owners
            sessions, last_ts, by_day = cache.get(owners.get(path, ""), HashUsage(0, None, {}))

            root, name, transient = self.resolve_group(path)
            group = groups.get(root)
            if group is None:
                group = groups[root] = Group(
                    root, name, transient=transient, new_bucket=self._pricing.new_bucket
                )

            group.sessions += sessions
            group.exists = group.exists or exists
            if last_ts is not None and (group.last_ts is None or last_ts > group.last_ts):
                group.last_ts = last_ts

            for day, by_model in by_day.items():
                for model, b in by_model.items():
                    group.total.merge(b)
                    totals_by_model[model].merge(b)
                    totals_by_day_by_model[day][model].merge(b)

        rows: list[ProjectRow] = []
        for group in groups.values():
            proj_cost = None if group.total.cost_unknown else group.total.cost
            if group.sessions > 0 or group.exists:
                rows.append(
                    {
                        "path": group.root,
                        "name": group.name,
                        "transient": group.transient,
                        "exists": group.exists,
                        "sessions": group.sessions,
                        "last_ts": group.last_ts,
                        "total": group.total,
                        "cost": proj_cost,
                    }
                )

        rows.sort(key=lambda r: r["cost"] if r["cost"] is not None else -1.0, reverse=True)  # pyrefly: ignore [implicit-any-lambda]
        return AggregateResult(
            rows,
            dict(totals_by_model),
            {d: dict(m) for d, m in totals_by_day_by_model.items()},
        )

    def resolve_window(
        self,
        from_date: date | None,
        until_date: date | None,
        days: int | None,
        *,
        days_given: bool,
    ) -> tuple[str | None, str | None] | WindowError:
        """
        Resolve parsed ``--from``/``--until``/``--days`` values into inclusive ISO bounds.

        *days_given* distinguishes ``--days 0`` (given, meaning unlimited) from the flag
        being absent, which a bare int cannot express. At most two of the three may be
        given.

        The resolution table, all bounds inclusive:

        * nothing → the last ``DEFAULT_DAYS`` days, ending today
        * ``--from`` alone → ``[from, today]``
        * ``--until`` alone → ``DEFAULT_DAYS`` ending at ``until``
        * ``--days N`` alone → ``[today-(N-1), today]``
        * ``--days 0`` alone → all time
        * a bound plus ``--days N`` → the N-day span anchored at that bound
        * a bound plus ``--days 0`` → open on the other side
        """
        if from_date is not None and until_date is not None and days_given:
            return WindowError("at most two of --from, --until, --days may be given")

        # A count of 0 means "unlimited" — it imposes no bound on the open side.
        lo, hi = self._combine_bounds(from_date, until_date, days or None, days_given=days_given)

        # The date.min / date.max sentinels mark open sides; collapse them back to None.
        lo_iso = None if lo == date.min else lo.isoformat()
        hi_iso = None if hi == date.max else hi.isoformat()
        if lo_iso is not None and hi_iso is not None and lo_iso > hi_iso:
            return WindowError("--from date is after --until date")
        return lo_iso, hi_iso

    def _combine_bounds(
        self,
        from_date: date | None,
        until_date: date | None,
        days_bound: int | None,
        *,
        days_given: bool,
    ) -> tuple[date, date]:
        """
        Apply the resolution table to already-parsed specs, returning ``(lo, hi)`` dates.

        ``days_bound`` is None for "no count" -- flag absent *or* the unlimited
        ``--days 0`` -- and *days_given* tells those apart. Open sides come back as the
        ``date.min``/``date.max`` sentinels.
        """
        today = get_day(self.now_utc(), DAY_START_HOURS)
        # Bounds are inclusive on both sides, so an N-day window offsets by N-1.
        # ``--days 0`` (days_given but no count) means unlimited — timedelta.max
        # saturates the bare side to an open sentinel via _shift.
        if days_given:
            span = timedelta(days=days_bound - 1) if days_bound else timedelta.max
        else:
            span = timedelta(days=DEFAULT_DAYS - 1)

        if from_date is not None and until_date is not None:
            return from_date, until_date
        if from_date is not None:
            # --from [--days N]: [from, from+(N-1)]; [from, open] for 0; else [from, today].
            return from_date, self._shift(from_date, span, sign=1) if days_given else today
        if until_date is not None:
            # --until [--days N]: [until-(N-1), until]; [open, until] for 0; else default span.
            return self._shift(until_date, span, sign=-1), until_date
        # Neither bound: the last N (or DEFAULT_DAYS) inclusive days, or all-time for 0.
        return self._shift(today, span, sign=-1), today

    @staticmethod
    def _shift(d: date, span: timedelta, *, sign: int) -> date:
        """
        ``date ± span``, clamped to ``[date.min, date.max]`` instead of raising.

        ``--days 0`` uses ``timedelta.max`` as its span, which overflows against a real
        date, so saturate to the open-side sentinel instead.
        """
        try:
            return d + sign * span
        except OverflowError:
            return date.max if sign > 0 else date.min

    @staticmethod
    def now_utc() -> datetime:
        """Return the current UTC instant — the seam tests freeze for deterministic windows."""
        return datetime.now(UTC)

    def build_report(self, projects: list[Path], args: UsageArgs) -> StatsReport:
        """
        Aggregate everything ``agent stats`` renders for one window, in one pass.

        *projects* is the full registry, unfiltered: telling "no project owns this log
        dir" from "the pattern hid its owner" needs every registered path. A hash whose
        owner the pattern hid is excluded outright rather than reappearing as orphaned.

        Orphaned spend folds into the shared totals and so shares the pattern gate --
        otherwise the projects table and the by-day totals would disagree.
        """
        show_orphaned = args.pattern is None or args.pattern.search(ORPHANED_LABEL) is not None
        selected = self.filter_projects(projects, args.pattern)

        owners = self.project_owners(projects)
        cache = self.usage_cache(
            from_iso=args.from_iso,
            until_iso=args.until_iso,
            refresh_pricing_data=args.refresh,
        )

        rows, totals_by_model, totals_by_day_by_model = self.aggregate_projects(
            selected, cache, owners
        )

        orphaned = None
        if show_orphaned:
            orphaned = self.aggregate_orphaned(
                cache,
                set(owners.values()),
                totals_by_model,
                totals_by_day_by_model,
            )

        return StatsReport(
            rows=[row for row in rows if row["sessions"] > 0],
            totals_by_model=totals_by_model,
            totals_by_day_by_model=totals_by_day_by_model,
            orphaned=orphaned,
            unrecorded=sum(b.unrecorded for b in totals_by_model.values()),
        )

    def filter_projects(self, projects: list[Path], pattern: re.Pattern[str] | None) -> list[Path]:
        """
        Select the registered projects matching *pattern*, or all of them when None.

        Matched against the whole registry path, not the display name, so a pattern can
        select by any path segment.
        """
        if pattern is None:
            return projects
        return [p for p in projects if pattern.search(str(p))]

    @cache  # noqa: B019 — StatsService is a process-lifetime singleton
    def resolve_group(self, path: Path) -> GroupResult:
        """
        Resolve the transient-project group a project path belongs to.

        Walks up from ``path`` along its **literal** components to the nearest
        ``.agent_stats_leaf``, and names the group after that marker's own directory --
        the file's content, if any, is not read.
        """
        for candidate in (path, *path.parents):
            marker = candidate / MARKER_NAME
            if marker.is_file():
                return GroupResult(
                    group_root=candidate, display_name=candidate.name, is_transient=True
                )
        return GroupResult(group_root=path, display_name=path.name, is_transient=False)

    def project_owners(self, projects: list[Path]) -> dict[Path, str]:
        """
        Map each registered project to the central ``<hash>`` directory it claims.

        The exact inverse of :meth:`orphaned_log_dirs`, from the same reachability rule,
        so the projects table and the ``<orphaned>`` row can never both claim or both
        disown one project's spend.

        Resolved through the project's ``.claude/litellm-logs`` symlink rather than by
        re-deriving the hash from the path: a deleted project has no symlink to follow,
        where re-hashing would happily produce the hash of a path that no longer exists.

        A project is absent from the result when it has no readable log directory, or
        when its logs live outside the central tree -- crediting it with a hash the tree
        does not have would let the same spend appear twice.
        """
        central = self._central_log_dirs()
        owners: dict[Path, str] = {}
        for project, resolved in self._claimed_log_dirs(projects).items():
            name = central.get(resolved)
            if name is not None:
                owners[project] = name
        return owners

    def orphaned_log_dirs(self, projects: list[Path]) -> list[Path]:
        """
        Central ``<hash>`` log dirs not reachable from a registered, existing project.

        Best-effort: filesystem errors are swallowed so a single bad entry can never
        break stats or the viewer.
        """
        claimed = set(self._claimed_log_dirs(projects).values())
        central = TOOL_DIR / LITELLM_LOGS_DIRNAME
        return sorted(
            central / name
            for resolved, name in self._central_log_dirs().items()
            if resolved not in claimed
        )

    @staticmethod
    def _claimed_log_dirs(projects: list[Path]) -> dict[Path, Path]:
        """
        Map each project to the log directory its ``.claude/litellm-logs`` resolves to.

        A project whose link is missing or unreadable is left out, which is what makes a
        deleted project's logs orphaned rather than still its own.
        """
        claimed: dict[Path, Path] = {}
        for project in projects:
            link = project / ".claude" / LITELLM_LOGS_DIRNAME
            try:
                if link.is_dir():
                    claimed[project] = link.resolve()
            except OSError:
                continue
        return claimed

    @staticmethod
    def _central_log_dirs() -> dict[Path, str]:
        """
        Map every central ``<hash>`` directory's resolved path to its hash name.

        Resolved paths are the keys because a project reaches its logs through a symlink,
        and that is the only form the two spellings agree on. The name rides along
        because it is what the index stores usage under.
        """
        central = TOOL_DIR / LITELLM_LOGS_DIRNAME
        try:
            children = list(central.iterdir())
        except OSError:
            return {}
        dirs: dict[Path, str] = {}
        for child in children:
            try:
                # Free: ``child`` came from ``iterdir()``, so ``.info`` answers from the
                # cached scandir dirent instead of a ``stat()``. The ``except OSError``
                # now guards only ``resolve()`` below — ``.info.is_dir()`` returns False
                # on error rather than raising.
                if not child.info.is_dir():
                    continue
                dirs[child.resolve()] = child.name
            except OSError:
                continue
        return dirs

    def aggregate_orphaned(
        self,
        cache: UsageCache,
        owned: set[str],
        totals_by_model: dict[str, Bucket],
        totals_by_day_by_model: dict[str, dict[str, Bucket]],
    ) -> OrphanedResult | None:
        """
        Aggregate the indexed usage of every hash no registered project owns.

        Everything in *cache* outside *owned* is spend whose project directory is gone or
        was never registered. That is real money, so each hash folds into the per-model
        and per-day totals exactly like a project.

        Taking *owned* rather than the project list makes this the complement of
        :meth:`aggregate_projects` by construction: no hash can be both or neither, so
        the tables cannot disagree about where a request went.
        """
        total = self._pricing.new_bucket()
        sessions = 0
        last_ts: datetime | None = None

        for project_hash, usage in sorted(cache.items()):
            if project_hash in owned:
                continue
            sessions += usage.sessions
            if usage.last_ts is not None and (last_ts is None or usage.last_ts > last_ts):
                last_ts = usage.last_ts
            for day, by_model in usage.by_day.items():
                for model, b in by_model.items():
                    total.merge(b)
                    # The totals are the plain dicts returned by aggregate_projects;
                    # orphaned logs may introduce a model/day not seen in any project,
                    # so create the bucket on demand rather than assuming it exists.
                    totals_by_model.setdefault(model, self._pricing.new_bucket()).merge(b)
                    totals_by_day_by_model.setdefault(day, {}).setdefault(
                        model, self._pricing.new_bucket()
                    ).merge(b)

        if sessions == 0:
            return None
        return {"sessions": sessions, "last_ts": last_ts, "total": total}

    def cleanup_scope(self) -> CleanupScope:
        """
        Survey what a cleanup would remove, without removing anything.

        Measured over exactly the dirs :meth:`run_cleanup` will be given, so the preview
        a user confirms describes the run that follows -- no re-walk, no TOCTOU gap.
        """
        orphaned_dirs = self.orphaned_log_dirs(self._config.read_project_paths())
        return CleanupScope(
            orphaned_dirs=orphaned_dirs,
            stale_paths=self._config.stale_project_paths(),
            freed_estimate=self.orphaned_disk_usage(orphaned_dirs),
        )

    def orphaned_disk_usage(self, orphaned_dirs: list[Path]) -> int:
        """
        Total bytes occupied by *orphaned_dirs*.

        Takes the list rather than recomputing it, so a caller reports a size for exactly
        the dirs it is about to delete.
        """
        return sum(directory_size(logs_dir) for logs_dir in orphaned_dirs)

    def run_cleanup(self, scope: CleanupScope) -> CleanupOutcome:
        """
        Delete the log dirs in *scope* and their index rows, then prune the registry.

        Directory first, index rows second, and the rows only where the removal
        succeeded. Deleting the rows first would blank a project's spend while its logs
        sat on disk waiting to be re-ingested.
        """
        result = self.delete_orphaned_logs(scope.orphaned_dirs)
        return CleanupOutcome(
            result=result, removed_paths=self._config.prune_stale_projects(scope.stale_paths)
        )

    def delete_orphaned_logs(self, orphaned_dirs: list[Path]) -> CleanupResult:
        """
        Delete each orphaned log dir, and forget the sessions it held.

        Takes *orphaned_dirs* from a prior :meth:`orphaned_log_dirs` call, so the caller
        shows counts before confirming and acts on that exact list after.

        Each dir is independent: a failed ``rmtree`` leaves that dir purely live, so it
        reappears next sweep. It is never counted twice, because a dir's index rows are
        dropped only once the dir itself is gone.

        Their spend goes with them, unarchived: the requests were indexed from files that
        no longer exist, so keeping the rows would report spend against a project the
        user has just asked to forget.
        """
        removed = 0
        freed = 0
        deleted_hashes: list[str] = []

        for logs_dir in orphaned_dirs:
            # Measured before removal — this is the figure reported as freed.
            size = directory_size(logs_dir)
            try:
                shutil.rmtree(logs_dir)
            except OSError:
                # Still live, still orphaned, still indexed. Try the remaining dirs.
                continue
            deleted_hashes.append(logs_dir.name)
            freed += size
            removed += 1

        self._ingest.delete_projects(deleted_hashes)
        return CleanupResult(removed=removed, freed_bytes=freed)
