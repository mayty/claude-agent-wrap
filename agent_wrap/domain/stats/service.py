# This file has been created with the assistance of an AI tool.
"""Token usage stats aggregation — domain service."""

from __future__ import annotations

import copy
import shutil
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from functools import cache, partial
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    DAY_START_HOURS,
    SCAN_PARALLEL_MIN_FILES,
    TOOL_DIR,
)
from agent_wrap.domain.stats.archive import (
    fold_records_into_archive,
    merge_archives,
    read_archive,
    write_archive,
)
from agent_wrap.domain.stats.constants import (
    CENTRAL_LOGS_DIRNAME,
    MARKER_NAME,
    ORPHANED_ARCHIVE_FILENAME,
)
from agent_wrap.domain.stats.format_utils import day_in_range
from agent_wrap.domain.stats.models import (
    AggregateResult,
    ArchivedBuckets,
    CleanupResult,
    DirResult,
    Group,
    GroupResult,
    OrphanedResult,
    ProjectRow,
    WorkUnit,
)
from agent_wrap.domain.stats.scan import (
    enumerate_session_files,
    fold_raw_to_buckets,
    plan_pool,
    price_buckets,
    scan_logs_dir,
    scan_one_file,
    scan_project,
    scan_session_file,
)
from agent_wrap.lib.daytime import get_day
from agent_wrap.lib.utils import directory_size

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.models import (
        ArchiveLeaf,
        RawFileResult,
        RawRecord,
        ScanCache,
    )


class StatsService:
    """Token usage stats aggregation service."""

    def __init__(self, pricing_service: PricingService, config_service: ConfigService) -> None:
        self._pricing = pricing_service
        self._config = config_service

    # ------------------------------------------------------------------
    # Per-file scan (used by the logs viewer's UsageTracker)
    # ------------------------------------------------------------------

    def scan_day_file(self, provider_name: str, messages_file: Path, day_key: str) -> Bucket:
        """
        Scan *messages_file* for records falling on *day_key* and return a priced Bucket.

        Used by ``UsageTracker`` in the logs domain to incrementally update
        today's usage totals without importing the stats scan internals.
        """
        _had_record, _last_ts, records = scan_session_file(
            provider_name,
            messages_file,
            from_iso=day_key,
            until_iso=day_key,
        )
        by_day, _by_source = fold_raw_to_buckets(records, self._pricing)
        price_buckets(by_day, self._pricing)

        combined = self._pricing.new_bucket()
        for bucket in by_day.get(day_key, {}).values():
            combined.merge(bucket)
        return combined

    # ------------------------------------------------------------------
    # Project aggregation
    # ------------------------------------------------------------------

    def aggregate_projects(
        self,
        projects: list[Path],
        *,
        from_iso: str | None = None,
        until_iso: str | None = None,
        scan_cache: ScanCache | None = None,
    ) -> AggregateResult:
        """
        Scan every project and roll its in-window buckets up into the render inputs.

        All restricted to the inclusive ``[from_iso, until_iso]`` window.
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
            sessions, last_ts, by_day, by_source, exists = scan_project(
                path, self._pricing, from_iso=from_iso, until_iso=until_iso, scan_cache=scan_cache
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

        rows.sort(key=lambda r: r["cost"] if r["cost"] is not None else -1.0, reverse=True)
        return AggregateResult(
            rows,
            dict(totals_by_model),
            {d: dict(m) for d, m in totals_by_day_by_model.items()},
            {s: dict(m) for s, m in totals_by_source.items()},
        )

    # Delegates for CLI orchestration------------------------------------

    def scan_log_dirs(
        self,
        logs_dirs: list[Path],
        *,
        from_iso: str | None,
        until_iso: str | None,
    ) -> ScanCache:
        """
        Scan many logs dirs and return a ``{logs_dir: folded_result}`` cache.

        Enumerates every session file across all dirs up front (cheap, metadata-only),
        then fans the per-file scans across a process pool when there are enough files
        to outweigh the pool's startup cost (see :data:`SCAN_PARALLEL_MIN_FILES`),
        otherwise scans serially in-process.  Workers extract raw token counts only;
        the master normalizes model names, folds into Buckets, and applies pricing.
        """
        # Enumerate (dir_index, provider, file) units.
        units: list[WorkUnit] = []
        for idx, logs_dir in enumerate(logs_dirs):
            for provider_name, messages_file in enumerate_session_files(logs_dir, from_iso):
                units.append(WorkUnit(idx, provider_name, messages_file))

        # Per-dir raw record buckets, keyed by dir_index.
        per_dir: list[list[RawFileResult]] = [[] for _ in logs_dirs]

        if len(units) < SCAN_PARALLEL_MIN_FILES:
            # Serial: small enough that a pool's fork/import cost would dominate.
            for idx, provider_name, messages_file in units:
                per_dir[idx].append(
                    scan_session_file(
                        provider_name,
                        messages_file,
                        from_iso=from_iso,
                        until_iso=until_iso,
                    )
                )
        else:
            workers, chunksize = plan_pool(len(units))
            scan_task = partial(
                scan_one_file,
                from_iso=from_iso,
                until_iso=until_iso,
            )
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for idx, result in pool.map(scan_task, units, chunksize=chunksize):
                    per_dir[idx].append(result)

        # Fold raw records into Buckets, then apply pricing -- all in the master.
        cache: ScanCache = {}
        for idx, logs_dir in enumerate(logs_dirs):
            # Collect all raw records across files for this dir.
            all_records: list[RawRecord] = []
            sessions = 0
            last_ts: datetime | None = None
            for had_record, ts, records in per_dir[idx]:
                if had_record:
                    sessions += 1
                if ts is not None and (last_ts is None or ts > last_ts):
                    last_ts = ts
                all_records.extend(records)

            by_day, by_source = fold_raw_to_buckets(all_records, self._pricing)
            price_buckets(by_day, self._pricing)
            price_buckets(by_source, self._pricing)
            cache[logs_dir] = DirResult(sessions, last_ts, by_day, by_source)
        return cache

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

    def orphaned_log_dirs(self, projects: list[Path]) -> list[Path]:
        """
        Central ``<hash>`` log dirs not reachable from a registered, existing project.

        Best-effort: filesystem errors are swallowed so a single bad entry can never
        break stats or the viewer.
        """
        reachable: set[Path] = set()
        for project in projects:
            link = project / ".claude" / CENTRAL_LOGS_DIRNAME
            try:
                if link.is_dir():
                    reachable.add(link.resolve())
            except OSError:
                continue

        central = TOOL_DIR / CENTRAL_LOGS_DIRNAME
        orphaned: list[Path] = []
        try:
            children = list(central.iterdir())
        except OSError:
            return []
        for child in children:
            try:
                if not child.is_dir():
                    continue
                if child.resolve() not in reachable:
                    orphaned.append(child)
            except OSError:
                continue
        return sorted(orphaned)

    def aggregate_orphaned(  # noqa: PLR0913
        self,
        projects: list[Path],
        totals_by_model: dict[str, Bucket],
        totals_by_day_by_model: dict[str, dict[str, Bucket]],
        totals_by_source: dict[str, dict[str, Bucket]] | None = None,
        *,
        from_iso: str | None = None,
        until_iso: str | None = None,
        scan_cache: ScanCache | None = None,
    ) -> OrphanedResult | None:
        """
        Aggregate central log dirs not reachable from any registered project.

        These are real spend whose project dir is gone, so each request is folded into
        the passed-in per-model and per-day totals (exactly like a project), and a
        single summary ``{"sessions", "last_ts", "total"}`` is returned for the
        synthetic ``<orphaned>`` row. Returns None when there are no orphaned sessions.

        When ``totals_by_source`` is given, orphaned spend is also folded into
        the per-source per-model breakdown so the verbose table stays consistent.
        When ``scan_cache`` is given, each orphaned dir's pre-scanned result is reused
        instead of scanning on demand (see :meth:`StatsService.scan_log_dirs`).
        """
        total = self._pricing.new_bucket()
        sessions = 0
        last_ts: datetime | None = None

        for logs_dir in self.orphaned_log_dirs(projects):
            if scan_cache is not None:
                d_sessions, d_last_ts, by_day, by_source = scan_cache.get(
                    logs_dir, DirResult(0, None, {}, {})
                )
            else:
                d_sessions, d_last_ts, by_day, by_source = scan_logs_dir(
                    logs_dir, self._pricing, from_iso=from_iso, until_iso=until_iso
                )
            sessions += d_sessions
            if d_last_ts is not None and (last_ts is None or d_last_ts > last_ts):
                last_ts = d_last_ts
            for day, by_model in by_day.items():
                for model, b in by_model.items():
                    total.merge(b)
                    # The totals are the plain dicts returned by _aggregate_projects;
                    # orphaned logs may introduce a model/day not seen in any project,
                    # so create the bucket on demand rather than assuming it exists.
                    totals_by_model.setdefault(model, self._pricing.new_bucket()).merge(b)
                    totals_by_day_by_model.setdefault(day, {}).setdefault(
                        model, self._pricing.new_bucket()
                    ).merge(b)
            if totals_by_source is not None:
                for source, by_model in by_source.items():
                    for model, b in by_model.items():
                        totals_by_source.setdefault(source, {}).setdefault(
                            model, self._pricing.new_bucket()
                        ).merge(b)

        if sessions == 0:
            return None
        return {"sessions": sessions, "last_ts": last_ts, "total": total}

    # ------------------------------------------------------------------
    # Cleanup — archiving and deleting orphaned log dirs
    # ------------------------------------------------------------------

    def orphaned_disk_usage(self, orphaned_dirs: list[Path]) -> int:
        """
        Total bytes occupied by *orphaned_dirs*.

        Takes the dir list rather than recomputing it, so the caller can report a
        size for exactly the same dirs it is about to delete.
        """
        return sum(directory_size(logs_dir) for logs_dir in orphaned_dirs)

    def archive_and_delete_orphaned(self, orphaned_dirs: list[Path]) -> CleanupResult:
        """
        Archive each orphaned dir's usage, then delete it.

        Takes *orphaned_dirs* from a prior :meth:`orphaned_log_dirs` call so the
        caller can show counts before confirming and act on that exact list after
        — no re-walk, no TOCTOU gap.

        Each dir is committed independently in two phases: its merged stats are
        written to a staging file *before* anything is deleted, and the staging
        file is promoted over the real archive only once the delete succeeded. A
        failed ``rmtree`` therefore leaves that dir purely live — it reappears in
        the next ``orphaned_log_dirs()`` and is never counted twice.

        Scanning is unwindowed (``from_iso=None``), which also disables the
        mtime culling in ``enumerate_session_files``, so all history is archived
        including records a normal stats window would skip.

        A failed promotion stops the run: that dir's logs are already gone while
        its stats live only in the staging file, so the caller must be told to
        move it into place by hand before more dirs are touched.
        """
        archive_path = AGENT_LAUNCHES_DIR / ORPHANED_ARCHIVE_FILENAME
        staging_path = archive_path.with_suffix(".new.json")
        combined = read_archive(archive_path)
        removed = 0
        freed = 0

        for logs_dir in orphaned_dirs:
            records: list[RawRecord] = []
            for provider_name, messages_file in enumerate_session_files(logs_dir, None):
                records.extend(
                    scan_session_file(
                        provider_name, messages_file, from_iso=None, until_iso=None
                    ).records
                )

            candidate = copy.deepcopy(combined)
            merge_archives(candidate, fold_records_into_archive(records, self._pricing))
            write_archive(staging_path, candidate)

            # Measured before removal — this is the figure reported as freed.
            size = directory_size(logs_dir)
            try:
                shutil.rmtree(logs_dir)
            except OSError:
                # Staging is never promoted, so this dir stays unarchived and
                # keeps being discovered live. Try the remaining dirs.
                continue
            try:
                staging_path.replace(archive_path)
            except OSError:
                return CleanupResult(
                    removed=removed,
                    freed_bytes=freed,
                    archive_path=archive_path,
                    staging_path=staging_path,
                    finalized=False,
                )
            combined = candidate
            freed += size
            removed += 1

        staging_path.unlink(missing_ok=True)
        return CleanupResult(
            removed=removed,
            freed_bytes=freed,
            archive_path=archive_path,
            staging_path=staging_path,
            finalized=True,
        )

    def aggregate_archived_orphaned(
        self,
        totals_by_model: dict[str, Bucket],
        totals_by_day_by_model: dict[str, dict[str, Bucket]],
        totals_by_source: dict[str, dict[str, Bucket]] | None = None,
        *,
        from_iso: str | None = None,
        until_iso: str | None = None,
    ) -> OrphanedResult | None:
        """
        Aggregate usage ``agent cleanup`` archived from dirs it already deleted.

        The read-side sibling of :meth:`aggregate_orphaned` — same merge shape,
        but sourced from the archive file instead of a filesystem walk. Folds into
        the caller's totals in place, exactly like ``aggregate_orphaned``, so the
        per-day and per-source tables account for archived spend too.

        Both day bucketing and pricing happen *here*, at read time: the archive
        stores raw UTC hours and no cost, so a later change to
        ``AGENT_DAY_START_UTC`` or to a provider's pricing table is reflected on
        the next run. Buckets are priced while still local and merged into the
        shared totals only afterwards — ``price_buckets`` adds to
        ``Bucket.cost``, so pricing anything already-priced would double-count.

        Returns None when nothing in the archive falls in range. The condition is
        message count, not session count: archived data has no session concept, so
        ``sessions`` is always 0 and testing it would always return None.
        """
        local_by_day, local_by_source, last_ts = self._read_archived_buckets(
            from_iso=from_iso, until_iso=until_iso
        )

        # Price while local, then merge — never the other way round.
        price_buckets(local_by_day, self._pricing)
        price_buckets(local_by_source, self._pricing)

        total = self._pricing.new_bucket()
        for day, by_model in local_by_day.items():
            for model, b in by_model.items():
                total.merge(b)
                totals_by_model.setdefault(model, self._pricing.new_bucket()).merge(b)
                totals_by_day_by_model.setdefault(day, {}).setdefault(
                    model, self._pricing.new_bucket()
                ).merge(b)
        if totals_by_source is not None:
            for source, by_model in local_by_source.items():
                for model, b in by_model.items():
                    totals_by_source.setdefault(source, {}).setdefault(
                        model, self._pricing.new_bucket()
                    ).merge(b)

        if total.msgs == 0:
            return None
        return {"sessions": 0, "last_ts": last_ts, "total": total}

    def _read_archived_buckets(
        self, *, from_iso: str | None, until_iso: str | None
    ) -> ArchivedBuckets:
        """
        Read the archive and materialize its in-window cells into unpriced buckets.

        Each ``(date, hour)`` cell is re-bucketed to a stats day here, using
        whatever ``DAY_START_HOURS`` is in force now rather than whatever was in
        force when the cell was archived — the reason the archive stores raw UTC
        hours. Returned buckets carry no cost; the caller prices them.
        """
        archive = read_archive(AGENT_LAUNCHES_DIR / ORPHANED_ARCHIVE_FILENAME)
        by_day: dict[str, dict[str, Bucket]] = {}
        by_source_totals: dict[str, dict[str, Bucket]] = {}
        last_ts: datetime | None = None

        for date_key, by_hour in archive.items():
            for hour_key, by_model in by_hour.items():
                dt = self._archived_hour_dt(date_key, hour_key)
                day_key = get_day(dt, DAY_START_HOURS).isoformat() if dt else "?"
                if not day_in_range(day_key, from_iso, until_iso):
                    continue
                if dt is not None and (last_ts is None or dt > last_ts):
                    last_ts = dt
                for model, by_source in by_model.items():
                    for source, leaf in by_source.items():
                        bucket = self._bucket_from_leaf(leaf)
                        by_day.setdefault(day_key, {}).setdefault(
                            model, self._pricing.new_bucket()
                        ).merge(bucket)
                        by_source_totals.setdefault(source, {}).setdefault(
                            model, self._pricing.new_bucket()
                        ).merge(bucket)

        return ArchivedBuckets(by_day, by_source_totals, last_ts)

    def _bucket_from_leaf(self, leaf: ArchiveLeaf) -> Bucket:
        """
        Build an unpriced Bucket from one archived leaf's raw counts.

        The explicit 5m/1h split is passed through so ``Bucket.add``'s
        flat-total fallback is never re-entered on this path — the tier split was
        already resolved when the record was archived.
        """
        cw_5m = leaf.get("cache_write_5m", 0)
        cw_1h = leaf.get("cache_write_1h", 0)
        return self._pricing.bucket_from_usage(
            {
                "input_tokens": leaf.get("input_tokens", 0),
                "output_tokens": leaf.get("output_tokens", 0),
                "cache_creation_input_tokens": cw_5m + cw_1h,
                "cache_read_input_tokens": leaf.get("cache_read", 0),
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw_5m,
                    "ephemeral_1h_input_tokens": cw_1h,
                },
            },
            msgs=leaf.get("msgs", 0),
            unrecorded=leaf.get("unrecorded", 0),
        )

    def _archived_hour_dt(self, date_key: str, hour_key: str) -> datetime | None:
        """
        Rebuild the UTC datetime an archived ``(date, hour)`` pair stands for.

        Returns None for the synthetic ``"?"`` keys, and for malformed keys — a
        hand-edited archive must not break stats.
        """
        try:
            year, month, day = (int(part) for part in date_key.split("-"))
            return datetime(year, month, day, int(hour_key), tzinfo=timezone.utc)
        except ValueError:
            return None

    def merge_orphaned_results(
        self, live: OrphanedResult | None, archived: OrphanedResult | None
    ) -> OrphanedResult | None:
        """
        Combine live-scanned and archived orphaned usage into one row's worth.

        Both sources render under the same synthetic ``<orphaned>`` label, so the
        display layer needs a single result. Passes through whichever side is
        present when the other is None; otherwise sums sessions, takes the newer
        timestamp, and merges both totals into a fresh bucket so neither input is
        mutated.
        """
        if live is None:
            return archived
        if archived is None:
            return live

        total = self._pricing.new_bucket()
        total.merge(live["total"])
        total.merge(archived["total"])
        timestamps = [ts for ts in (live["last_ts"], archived["last_ts"]) if ts is not None]
        return {
            "sessions": live["sessions"] + archived["sessions"],
            "last_ts": max(timestamps) if timestamps else None,
            "total": total,
        }
