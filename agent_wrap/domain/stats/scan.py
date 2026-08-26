# This file has been edited with the assistance of an AI tool.
"""Log-file scanning (serial + parallel) for the stats command."""

import json
import math
import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from agent_wrap.constants import DAY_START_HOURS, LITELLM_LOGS_DIRNAME
from agent_wrap.domain.stats.constants import UNKNOWN_TIME_KEY
from agent_wrap.domain.stats.cost import usage_source
from agent_wrap.domain.stats.format_utils import day_in_range
from agent_wrap.domain.stats.models import (
    AccumulatedRecord,
    DirResult,
    HourBuckets,
    HourKey,
    RawFileResult,
    RawRecord,
    ScanProjectResult,
)
from agent_wrap.lib.daytime import epoch_to_dt, get_day

if TYPE_CHECKING:
    from agent_wrap.domain.pricing.models import TokenUsage

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.models import (
        ScanCache,
        WorkUnit,
    )


def file_predates_range(messages_file: Path, from_iso: str | None) -> bool:
    """
    Report whether a log file can be culled (skipped unread) for the lower bound.

    LiteLLM message logs are append-only and written live, so every record's event
    time is ``<= the file's mtime``. If the file's mtime (as a host-local date) is
    before ``from_iso``, no record in it can be in range, so it is safe to skip
    without parsing. Only the lower bound is cullable this way — a recent mtime
    says nothing about a file's *earliest* record, so ``--until`` cannot cull via
    metadata. Returns False when ``from_iso`` is None (no lower bound) or on any
    stat error (fall through to parsing, which stays the authority).
    """
    if from_iso is None:
        return False
    try:
        mtime = messages_file.stat().st_mtime
    except OSError:
        return False
    mtime_day = epoch_to_dt(mtime)
    if mtime_day is None:
        return False
    return get_day(mtime_day, DAY_START_HOURS).isoformat() < from_iso


def enumerate_session_files(logs_dir: Path, from_iso: str | None) -> list[tuple[str, Path]]:
    """
    List a logs dir's ``(provider_name, messages.jsonl)`` units, mtime-culled.

    Walks the ``<provider>/<session>/`` layout shared by a project's
    ``.claude/litellm-logs`` symlink and a central orphaned ``<hash>`` dir, and
    drops files whose mtime predates the lower bound (see
    :func:`file_predates_range`) so culled files never become scan work. This is
    the cheap metadata-only pass; the costly per-file parsing happens in
    :func:`scan_session_file`, which lets the work be fanned out across processes.
    """
    units: list[tuple[str, Path]] = []
    try:
        provider_dirs = list(logs_dir.iterdir())
    except OSError:
        return units
    # ``.info`` on a path that came out of ``iterdir()`` answers from the cached
    # ``os.scandir`` dirent, so these two checks cost no ``stat()`` — which matters
    # because this loop runs once per session dir. Only paths *yielded by* iterdir
    # carry that cache; a constructed Path stats on first ``.info`` use instead
    # (which is why ``messages_file`` below still uses plain ``is_file()``).
    for provider_dir in provider_dirs:
        if not provider_dir.info.is_dir():
            continue
        provider_name = provider_dir.name
        try:
            session_dirs = provider_dir.iterdir()
        except OSError:
            continue
        for session_dir in session_dirs:
            if not session_dir.info.is_dir():
                continue
            messages_file = session_dir / "messages.jsonl"
            if not messages_file.is_file():
                continue
            if file_predates_range(messages_file, from_iso):
                continue
            units.append((provider_name, messages_file))
    return units


def accumulate_record(
    rec: dict[str, Any],
    provider_name: str,
    *,
    from_iso: str | None,
    until_iso: str | None,
) -> AccumulatedRecord:
    """
    Extract usage from one log record and return raw data for master folding.

    Extracts raw token counts without any pricing-domain work — returns an
    ``AccumulatedRecord`` so the caller can normalize model names and fold
    into Buckets using a ``PricingService``. Workers never touch Buckets or
    pricing code.
    """
    if rec.get("status") != "success":
        return AccumulatedRecord(
            accumulated=False,
            ts=None,
            day_key=None,
            display_model=None,
            usage=None,
            source="",
            unrecorded=False,
        )
    model = rec.get("model")
    if not model:
        return AccumulatedRecord(
            accumulated=False,
            ts=None,
            day_key=None,
            display_model=None,
            usage=None,
            source="",
            unrecorded=False,
        )

    ts = epoch_to_dt((rec.get("timing") or {}).get("start"))
    day_key = get_day(ts, DAY_START_HOURS).isoformat() if ts else UNKNOWN_TIME_KEY
    if not day_in_range(day_key, from_iso, until_iso):
        return AccumulatedRecord(
            accumulated=False,
            ts=None,
            day_key=None,
            display_model=None,
            usage=None,
            source="",
            unrecorded=False,
        )

    clean_model = model.rsplit("/", 1)[-1]
    display_model = f"{provider_name}/{clean_model}"

    # Extract flat token counts from the response — simple dict access only,
    # no pricing-domain imports. The master will do proper tier attribution
    # via PricingService when folding into Buckets.
    response = rec.get("response")
    resp_usage: dict[str, Any] | None = (
        response.get("usage") if isinstance(response, dict) else None
    )
    if not isinstance(resp_usage, dict):
        resp_usage = {}
    usage: TokenUsage = {
        "input_tokens": resp_usage.get("input_tokens") or resp_usage.get("prompt_tokens") or 0,
        "output_tokens": resp_usage.get("output_tokens")
        or resp_usage.get("completion_tokens")
        or 0,
        "cache_creation_input_tokens": resp_usage.get("cache_creation_input_tokens") or 0,
        "cache_read_input_tokens": resp_usage.get("cache_read_input_tokens") or 0,
        "cache_creation": resp_usage.get("cache_creation") or {},
    }

    source = usage_source(rec)
    unrecorded = source == "unrecoverable"
    return AccumulatedRecord(
        accumulated=True,
        ts=ts,
        day_key=day_key,
        display_model=display_model,
        usage=usage,
        source=source,
        unrecorded=unrecorded,
    )


def scan_session_file(
    provider_name: str,
    messages_file: Path,
    *,
    from_iso: str | None,
    until_iso: str | None,
) -> RawFileResult:
    """
    Scan one ``messages.jsonl`` line-by-line, returning raw records.

    Returns ``(had_record, last_ts, records)`` where *records* is a list of
    ``(day_key, display_model, usage, unrecorded)`` tuples.  Model names are
    NOT normalized and Buckets are NOT constructed — the caller does both
    using a ``PricingService``. This keeps workers free of pricing-domain
    imports so the architecture validator stays clean.
    """
    records: list[RawRecord] = []
    last_ts: datetime | None = None
    had_record = False
    try:
        with messages_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                accumulated, ts, day_key, display_model, usage, source, unrecorded = (
                    accumulate_record(
                        rec,
                        provider_name,
                        from_iso=from_iso,
                        until_iso=until_iso,
                    )
                )
                if accumulated:
                    had_record = True
                    # All fields are non-None when accumulated is True.
                    assert day_key is not None
                    assert display_model is not None
                    assert usage is not None
                    records.append(RawRecord(day_key, display_model, usage, source, unrecorded, ts))
                if ts is not None and (last_ts is None or ts > last_ts):
                    last_ts = ts
    except OSError:
        return RawFileResult(had_record=False, last_ts=None, records=[])
    return RawFileResult(had_record=had_record, last_ts=last_ts, records=records)


def fold_raw_to_buckets(
    records: list[RawRecord],
    pricing: PricingService,
) -> tuple[HourBuckets, HourBuckets]:
    """
    Fold raw worker records into hour-keyed Bucket dicts using *pricing*.

    Normalizes model names and constructs Buckets via ``pricing.new_bucket()``,
    so all pricing-domain work stays in the master process. The raw UTC weekday
    and hour are preserved on the bucket key so ``price_buckets`` can price each
    hour at its own rate before collapsing the axis.

    Returns ``(by_day, by_source)`` where *by_day* is
    ``{day: {hour: {model: Bucket}}}`` and *by_source* is
    ``{source: {hour: {model: Bucket}}}``. Both inner dicts use
    ``"provider/model"`` display-model keys, so ``price_buckets`` works on either.
    """
    by_day = defaultdict(lambda: defaultdict(lambda: defaultdict(pricing.new_bucket)))
    by_source = defaultdict(lambda: defaultdict(lambda: defaultdict(pricing.new_bucket)))
    for rec in records:
        provider, _, model = rec.display_model.partition("/")
        norm_model = pricing.normalize_model(model) or model
        norm_display = f"{provider}/{norm_model}"
        hour_key = (
            HourKey(rec.ts.weekday(), rec.ts.hour) if rec.ts is not None else HourKey(None, None)
        )
        by_day[rec.day_key][hour_key][norm_display].add(rec.usage, 0.0, unrecorded=rec.unrecorded)
        by_source[rec.source][hour_key][norm_display].add(rec.usage, 0.0, unrecorded=rec.unrecorded)
    return (
        {d: {h: dict(m) for h, m in by_hour.items()} for d, by_hour in by_day.items()},
        {s: {h: dict(m) for h, m in by_hour.items()} for s, by_hour in by_source.items()},
    )


def merge_by_day(dst: dict[str, dict[str, Bucket]], src: dict[str, dict[str, Bucket]]) -> None:
    """Merge one ``by_day[day][key] -> Bucket`` map into another in place."""
    for day, by_key in src.items():
        dst_day = dst.setdefault(day, {})
        for key, bucket in by_key.items():
            existing = dst_day.get(key)
            if existing is None:
                dst_day[key] = bucket
            else:
                existing.merge(bucket)


def price_buckets(
    buckets: HourBuckets,
    pricing: PricingService,
    *,
    refresh_pricing_data: bool = False,
) -> dict[str, dict[str, Bucket]]:
    """
    Price every Bucket in *buckets*, then collapse the hour/weekday axis.

    Each ``(outer_key, (weekday, hour), model)`` bucket is priced at its own UTC
    weekday and hour, then merged into the ``{outer_key: {model: Bucket}}`` shape
    the rest of the stats pipeline consumes. Pricing before collapsing is what
    keeps a day's usage from being priced at a single representative instant.
    """
    collapsed: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(pricing.new_bucket))
    for outer_key, by_hour in buckets.items():
        for hour_key, by_model in by_hour.items():
            weekday, hour = hour_key
            for display_model, bucket in by_model.items():
                provider, _, model = display_model.partition("/")
                usage: TokenUsage = {
                    "input_tokens": bucket.in_,
                    "output_tokens": bucket.out,
                    "cache_creation_input_tokens": bucket.cw,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": bucket.cw_5m,
                        "ephemeral_1h_input_tokens": bucket.cw_1h,
                    },
                    "cache_read_input_tokens": bucket.cr,
                }
                cost = pricing.compute_cost(
                    provider,
                    model,
                    usage=usage,
                    hour=hour,
                    weekday=weekday,
                    refresh_pricing_data=refresh_pricing_data,
                )
                if cost is None:
                    bucket.cost_unknown = True
                else:
                    bucket.cost += cost
                collapsed[outer_key][display_model].merge(bucket)
    return {k: dict(v) for k, v in collapsed.items()}


# Parallel scan
#
# The scan is embarrassingly parallel per session file, and json.loads (CPU-bound
# C that holds the GIL) dominates it, so a process pool — not threads — is what
# wins. Workers extract raw token counts only; the master process normalizes model
# names, folds into Buckets, and applies pricing afterward. This eliminates the
# need for any pricing-domain imports in workers — no globals, no initializer,
# no cross-domain dependencies.


def plan_pool(nfiles: int) -> tuple[int, int]:
    """
    Choose ``(workers, chunksize)`` for a parallel scan of ``nfiles`` files.

    Sized to the machine and the workload, validated against a chunksize-by-pool
    sweep on a 25.5K-record dataset:
      * workers — ``min(process_cpu_count, 8, ceil(nfiles / 16))``. Decode
        saturates ~8 workers (16 was no faster), so 8 is the cap; it also scales
        *down* on few-core hosts and small datasets (no point forking 8 for 20
        files). ``process_cpu_count`` rather than ``cpu_count`` so an affinity-
        restricted host (``taskset``, a cpuset) is not over-forked with workers
        that only contend for cores this process may not use; it also honours
        ``PYTHON_CPU_COUNT`` / ``-X cpu_count``, so the pool size is overridable
        without a code change.
      * chunksize — ``max(1, min(8, nfiles // (workers * 4)))``, ≈4 chunks per
        worker. ``map`` dispatches chunks lazily as workers free up, so several
        small chunks per worker keep load balanced when a few sessions are far
        larger than the rest; the [1, 8] clamp matches the sweep's flat optimum.
    """
    cpu = os.process_cpu_count() or 1
    workers = max(1, min(cpu, 8, math.ceil(nfiles / 16)))
    chunksize = max(1, min(8, nfiles // (workers * 4)))
    return workers, chunksize


def scan_one_file(
    unit: WorkUnit,
    *,
    from_iso: str | None,
    until_iso: str | None,
) -> tuple[int, RawFileResult]:
    """
    Pool task: scan one file, extracting raw token counts only (no pricing).

    Window bounds are bound via :func:`functools.partial` so every task sees the
    same range without globals or an initializer.
    """
    dir_index, provider_name, messages_file = unit
    result = scan_session_file(
        provider_name,
        messages_file,
        from_iso=from_iso,
        until_iso=until_iso,
    )
    return dir_index, result


def scan_logs_dir(
    logs_dir: Path,
    pricing: PricingService,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
    refresh_pricing_data: bool = False,
) -> DirResult:
    """
    Scan a LiteLLM logs dir (``<provider>/<session>/messages.jsonl``) line-by-line,
    costing each in-window request as it is read.

    Only records whose day falls within ``[from_iso, until_iso]`` are counted, and
    a session is counted only when it contributed at least one such record. Files
    whose mtime predates the lower bound are skipped unread (see
    :func:`file_predates_range`).

    *by_source* mirrors the model breakdown but is additionally keyed by usage
    source (see :func:`usage_source`), feeding the verbose breakdown.

    Works on both a project's ``.claude/litellm-logs`` symlink and a central
    orphaned ``<hash>`` dir, since they share the same internal layout. This is the
    serial path; the parallel scan in :meth:`StatsService.scan_log_dirs` fans the same per-file core
    (:func:`scan_session_file`) across processes and folds raw records afterward.
    """
    all_records: list[RawRecord] = []
    sessions = 0
    last_ts: datetime | None = None
    for provider_name, messages_file in enumerate_session_files(logs_dir, from_iso):
        had_record, ts, records = scan_session_file(
            provider_name,
            messages_file,
            from_iso=from_iso,
            until_iso=until_iso,
        )
        if had_record:
            sessions += 1
        if ts is not None and (last_ts is None or ts > last_ts):
            last_ts = ts
        all_records.extend(records)

    by_day_hour, by_source_hour = fold_raw_to_buckets(all_records, pricing)
    by_day = price_buckets(by_day_hour, pricing, refresh_pricing_data=refresh_pricing_data)
    by_source = price_buckets(by_source_hour, pricing, refresh_pricing_data=refresh_pricing_data)
    return DirResult(sessions, last_ts, by_day, by_source)


def scan_project(  # noqa: PLR0913
    path: Path,
    pricing: PricingService,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
    scan_cache: ScanCache | None = None,
    refresh_pricing_data: bool = False,
) -> ScanProjectResult:
    """
    Scan one project's LiteLLM logs.

    ``exists`` is False when the project's ``.claude/litellm-logs`` is gone (a
    deleted project / stale registry entry), in which case nothing is scanned.
    When ``scan_cache`` is given, this dir's pre-scanned result is reused instead
    of scanning on demand (see :meth:`StatsService.scan_log_dirs`).
    """
    logs_dir = path / ".claude" / LITELLM_LOGS_DIRNAME
    if not logs_dir.is_dir():
        return ScanProjectResult(sessions=0, last_ts=None, by_day={}, by_source={}, exists=False)
    if scan_cache is not None:
        sessions, last_ts, by_day, by_source = scan_cache.get(logs_dir, DirResult(0, None, {}, {}))
    else:
        sessions, last_ts, by_day, by_source = scan_logs_dir(
            logs_dir,
            pricing,
            from_iso=from_iso,
            until_iso=until_iso,
            refresh_pricing_data=refresh_pricing_data,
        )
    return ScanProjectResult(
        sessions=sessions, last_ts=last_ts, by_day=by_day, by_source=by_source, exists=True
    )
