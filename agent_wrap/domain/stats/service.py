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
    """Token usage stats aggregation service."""

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

    # ------------------------------------------------------------------
    # Reading the index
    # ------------------------------------------------------------------

    def day_totals(self, day_key: str) -> Bucket:
        """
        Return one stats day's whole priced usage, across every project on the host.

        What ``usage.json`` holds, and the reason the viewer no longer re-reads a log
        file per append: the cost of this is set by the number of *cells* in one day --
        a few dozen -- rather than by the size of the files the day's requests were
        written to.
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

        One aggregate serves the project rows, the shared totals and the orphaned row,
        so those three can never disagree about a request. The window is applied in SQL
        as an hour-bucket range and again per cell as a day rule -- see
        :func:`hour_bounds` for why both, and why that is not redundant.
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

    # ------------------------------------------------------------------
    # Project aggregation
    # ------------------------------------------------------------------

    def aggregate_projects(
        self,
        projects: list[Path],
        cache: UsageCache,
        owners: dict[Path, str],
    ) -> AggregateResult:
        """
        Roll each project's folded usage up into the four render inputs.

        *cache* is one whole read of the index, keyed by project hash, and *owners* says
        which hash each project claims (see :meth:`project_owners`). A project absent
        from *owners* has no log directory, and a hash absent from *cache* contributed
        nothing in the window; either way the project gets an empty
        :class:`HashUsage` rather than a special case.

        The window is already applied -- it was applied when *cache* was read -- so
        this method takes no bounds. That is the point of reading once: the project
        rows and the shared totals are folded from the same cells rather than from two
        passes that could disagree.
        """
        groups: dict[Path, Group] = {}
        totals_by_model: dict[str, Bucket] = defaultdict(self._pricing.new_bucket)
        totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(
            lambda: defaultdict(self._pricing.new_bucket)
        )
        totals_by_source: dict[str, dict[str, Bucket]] = defaultdict(
            lambda: defaultdict(self._pricing.new_bucket)
        )

        for path in projects:
            exists = path in owners
            sessions, last_ts, by_day, by_source = cache.get(
                owners.get(path, ""), HashUsage(0, None, {}, {})
            )

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

            for source, by_model in by_source.items():
                for model, b in by_model.items():
                    totals_by_source[source][model].merge(b)

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
            {s: dict(m) for s, m in totals_by_source.items()},
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
        given. Returns ``(from_iso, until_iso)`` with None for an open side, or a
        :class:`WindowError` naming the problem.

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

        ``days_bound`` is the positive day count, or None for "no count" (flag absent
        *or* the unlimited ``--days 0``); *days_given* tells those apart so a bare side
        stays open for ``--days 0`` but defaults to today/DEFAULT_DAYS otherwise. Open
        sides come back as the ``date.min``/``date.max`` sentinels.
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

        The unlimited ``--days 0`` case uses ``timedelta.max`` as its span; adding or
        subtracting that from a real date overflows, so saturate to the open-side
        sentinel (``date.max`` forward, ``date.min`` backward).
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

        *projects* is the full registry, unfiltered — telling "no project owns this log
        dir" from "the pattern hid its owner" needs every registered path, so filtering
        happens inside. A hash whose owner the pattern hid is excluded outright rather
        than reappearing as orphaned.

        Orphaned spend folds into the shared totals, so it shares the pattern gate:
        folding in spend whose row is suppressed would break the agreement between the
        projects table and the by-day totals.
        """
        show_orphaned = args.pattern is None or args.pattern.search(ORPHANED_LABEL) is not None
        selected = self.filter_projects(projects, args.pattern)

        owners = self.project_owners(projects)
        cache = self.usage_cache(
            from_iso=args.from_iso,
            until_iso=args.until_iso,
            refresh_pricing_data=args.refresh,
        )

        rows, totals_by_model, totals_by_day_by_model, totals_by_source = self.aggregate_projects(
            selected, cache, owners
        )

        orphaned = None
        if show_orphaned:
            orphaned = self.aggregate_orphaned(
                cache,
                set(owners.values()),
                totals_by_model,
                totals_by_day_by_model,
                totals_by_source,
            )

        return StatsReport(
            rows=[row for row in rows if row["sessions"] > 0],
            totals_by_model=totals_by_model,
            totals_by_day_by_model=totals_by_day_by_model,
            totals_by_source=totals_by_source,
            orphaned=orphaned,
            unrecorded=sum(b.unrecorded for b in totals_by_model.values()),
        )

    def filter_projects(self, projects: list[Path], pattern: re.Pattern[str] | None) -> list[Path]:
        """
        Select the registered projects matching *pattern*, or all of them when None.

        Matched against the whole recorded registry path, not the display name, so a
        pattern can select by any path segment.
        """
        if pattern is None:
            return projects
        return [p for p in projects if pattern.search(str(p))]

    @cache  # noqa: B019 — StatsService is a process-lifetime singleton
    def resolve_group(self, path: Path) -> GroupResult:
        """
        Resolve the transient-project group a project path belongs to.

        Walks up from ``path`` (inclusive) along its **literal** components looking
        for the nearest ``.agent_stats_leaf``. The group is always named after the
        marker's own directory — the marker file's content, if any, is not read.
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

        The exact inverse of :meth:`orphaned_log_dirs`: a hash claimed here is a hash
        that is not orphaned, and both answers come from the same reachability rule, so
        the projects table and the ``<orphaned>`` row can never both claim one
        project's spend or both disown it.

        Reachability is resolved through the project's own
        ``.claude/litellm-logs`` symlink rather than by re-deriving the hash from the
        project path. Two reasons: a project whose directory is gone has no symlink to
        follow and so is correctly treated as having no logs, where re-hashing would
        happily produce the hash of a path that no longer exists; and a project whose
        symlink points somewhere unexpected is disowned rather than credited with
        whatever it points at.

        A project absent from the result has no readable log directory. So is one whose
        logs live outside the central tree, since the index only ever holds what the
        sidecars wrote under ``TOOL_DIR/litellm-logs`` -- and crediting a project with a
        hash the central tree does not have would let the same spend appear twice.
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

        Projects whose link is missing or unreadable are left out, which is what makes a
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

        Resolved paths are the keys because that is the only form two spellings of one
        directory agree on -- a project reaches its logs through a symlink, and the
        comparison against a central child has to survive that. The name is carried
        alongside because it is the key the index stores usage under.

        Best-effort: an unreadable central tree yields nothing rather than raising, so a
        permissions problem degrades stats instead of breaking it.
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
        totals_by_source: dict[str, dict[str, Bucket]] | None = None,
    ) -> OrphanedResult | None:
        """
        Aggregate the indexed usage of every hash no registered project owns.

        *owned* is the set of hashes claimed by the selected projects; everything else
        in *cache* is spend whose project directory is gone or was never registered.
        That is real money, so each hash is folded into the passed-in per-model and
        per-day totals exactly like a project, and a single summary
        ``{"sessions", "last_ts", "total"}`` is returned for the synthetic
        ``<orphaned>`` row. Returns None when nothing is orphaned.

        Taking *owned* rather than the project list is what makes this the complement of
        :meth:`aggregate_projects` by construction: no hash can be both, and none can be
        neither, so the tables cannot disagree about where a request went.

        When ``totals_by_source`` is given, orphaned spend is also folded into the
        per-source per-model breakdown so the verbose table stays consistent.
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
            if totals_by_source is not None:
                for source, by_model in usage.by_source.items():
                    for model, b in by_model.items():
                        totals_by_source.setdefault(source, {}).setdefault(
                            model, self._pricing.new_bucket()
                        ).merge(b)

        if sessions == 0:
            return None
        return {"sessions": sessions, "last_ts": last_ts, "total": total}

    # ------------------------------------------------------------------
    # Cleanup — deleting orphaned log dirs and their index rows
    # ------------------------------------------------------------------

    def cleanup_scope(self) -> CleanupScope:
        """
        Survey what a cleanup would remove, without removing anything.

        The size is measured now, over exactly the dirs :meth:`run_cleanup` will be
        given, so the preview a user confirms describes the run that follows — no
        re-walk, no TOCTOU gap between the two.
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

        Takes the dir list rather than recomputing it, so a caller can report a size
        for exactly the same dirs it is about to delete.
        """
        return sum(directory_size(logs_dir) for logs_dir in orphaned_dirs)

    def run_cleanup(self, scope: CleanupScope) -> CleanupOutcome:
        """
        Delete the log dirs in *scope* and their index rows, then prune the registry.

        Two deletes per orphaned project, deliberately in this order: the directory
        first, its index rows second, and the rows only for directories whose removal
        actually succeeded. Neither half can leave the host inconsistent for long -- a
        failed ``rmtree`` leaves rows the next ingest simply keeps using, and a failed
        ``DELETE`` leaves rows for a directory the viewer's next reconcile reaps -- but
        deleting the rows first would blank a project's spend while its logs were still
        on disk waiting to be re-ingested.
        """
        result = self.delete_orphaned_logs(scope.orphaned_dirs)
        return CleanupOutcome(
            result=result, removed_paths=self._config.prune_stale_projects(scope.stale_paths)
        )

    def delete_orphaned_logs(self, orphaned_dirs: list[Path]) -> CleanupResult:
        """
        Delete each orphaned log dir, and forget the sessions it held.

        Takes *orphaned_dirs* from a prior :meth:`orphaned_log_dirs` call so the caller
        can show counts before confirming and act on that exact list after — no
        re-walk, no TOCTOU gap.

        Each dir is independent: a ``rmtree`` that fails leaves that dir purely live, so
        it reappears in the next ``orphaned_log_dirs()`` and its spend keeps showing up
        under ``<orphaned>``. It is never counted twice, because the index rows for a
        dir are dropped only once the dir itself is gone.

        Their spend goes with them. There is no archive: the requests were indexed from
        files that no longer exist, so keeping the rows would report spend against a
        project the user has just asked to forget, and keeping a JSON side-copy of it
        (which is what this used to do) meant a second format, a two-phase promotion,
        and a read path nothing else used.
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
