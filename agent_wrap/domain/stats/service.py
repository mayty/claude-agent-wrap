# This file has been created with the assistance of an AI tool.
"""Token usage stats aggregation — domain service."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import cache, partial
from typing import TYPE_CHECKING

from agent_wrap.constants import SCAN_PARALLEL_MIN_FILES, TOOL_DIR
from agent_wrap.domain.stats.constants import CENTRAL_LOGS_DIRNAME, MARKER_NAME
from agent_wrap.domain.stats.models import (
    AggregateResult,
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

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.models import RawFileResult, RawRecord, ScanCache


class StatsService:
    """Token usage stats aggregation service."""

    def __init__(self, pricing_service: PricingService, config_service: ConfigService) -> None:
        self._pricing = pricing_service
        self._config = config_service

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
        for the nearest ``.agent_stats_leaf``.
        """

        # Inline of _read_marker_name to avoid importing a _-prefixed name.
        def _read_marker_name(marker: Path) -> str | None:
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line:
                    return line
            return None

        for candidate in (path, *path.parents):
            marker = candidate / MARKER_NAME
            if marker.is_file():
                name = _read_marker_name(marker)
                display_name = name if name is not None else candidate.name
                return GroupResult(
                    group_root=candidate, display_name=display_name, is_transient=True
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
