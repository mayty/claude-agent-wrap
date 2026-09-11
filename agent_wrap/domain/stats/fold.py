# This file has been edited with the assistance of an AI tool.
"""
Folding the index's usage cells into priced buckets for the stats command.

Nothing here reads a file. The whole population arrives as :class:`UsageCell`s from one
aggregate over ``logs.db`` -- see ``agent_wrap/domain/logs/ingest.py`` for why no
consumer parses a log file any more -- and this module's job is to turn those cells into
the ``{outer key: {model: Bucket}}`` shape the renderer consumes, priced per hour.

The two-stage shape survives from the file scan and is still load-bearing. A cell is
folded into an *unpriced* bucket keyed by its UTC ``(weekday, hour)``, and only then is
:func:`price_buckets` allowed to collapse that axis -- because a provider may charge a
different rate by time of day, and pricing a whole day at one representative instant
would silently misprice it.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_wrap.constants import DAY_START_HOURS, UNRECOVERABLE_SOURCE
from agent_wrap.domain.stats.constants import SECONDS_PER_HOUR, UNKNOWN_TIME_KEY
from agent_wrap.domain.stats.format_utils import day_in_range
from agent_wrap.domain.stats.models import HashUsage, HourBuckets, HourKey
from agent_wrap.lib.daytime import get_day

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agent_wrap.domain.pricing.models import Bucket, TokenUsage
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.infrastructure.logs.models import UsageCell


def hour_bucket_dt(hour_bucket: int) -> datetime:
    """
    Return the UTC instant an hour bucket starts at.

    The inverse of the index's ``started_at_us / 3600000000`` expression, and the only
    place a bucket is turned back into a calendar. The bucket is a count of whole hours
    since the epoch, so this is exact rather than approximate -- and it is deliberately
    the *start* of the hour, since every derived field (the weekday, the UTC hour, the
    ``DAY_START_HOURS``-shifted stats day) is constant across the hour it names.
    """
    return datetime.fromtimestamp(hour_bucket * SECONDS_PER_HOUR, tz=UTC)


def usage_from_cell(cell: UsageCell) -> TokenUsage:
    """
    Present one cell's token counts as the usage shape the pricing domain charges.

    ``cache_creation`` is left empty on purpose, which routes the whole
    ``cache_write_tokens`` total through ``Bucket.add``'s last-resort rule and charges
    it at the 5-minute rate. The index *does* store the 5m/1h split, and this
    deliberately does not spend it: no consumer read that split before the index
    existed either -- ``Bucket.add`` only ever looked at ``usage.cache_creation``, which
    0 of 45,403 records in the log tree populate -- so honouring it here would change
    what users are told they spent as a side effect of a storage change. Correcting the
    tier is its own change; ``cache_write_5m`` / ``cache_write_1h`` are what make it
    possible without re-ingesting.
    """
    return {
        "input_tokens": cell.input_tokens,
        "output_tokens": cell.output_tokens,
        "cache_creation_input_tokens": cell.cache_write_tokens,
        "cache_read_input_tokens": cell.cache_read,
        "cache_creation": {},
    }


def fold_cells(
    cells: Iterable[UsageCell],
    pricing: PricingService,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
    refresh_pricing_data: bool = False,
) -> dict[str, HashUsage]:
    """
    Fold usage cells into one priced :class:`HashUsage` per project hash.

    The hash is the outermost key because it is the only project identity the index
    holds -- resolving a hash to a path (or to no path at all, which is what makes a
    session orphaned) is a question about the registry *now*, and belongs to the caller.

    Each cell contributes to both breakdowns, keyed by stats day and by usage source, so
    the verbose table and the default table are computed in one pass over the same
    population and cannot disagree. ``day_in_range`` is applied per cell even though the
    query is already bounded: the bound is an hour bucket and this is the day rule the
    rest of the domain uses, so re-stating it here is what keeps the ``"?"`` bucket's
    all-time-only behaviour in one place.
    """
    by_hash_day: dict[str, HourBuckets] = defaultdict(dict)
    by_hash_source: dict[str, HourBuckets] = defaultdict(dict)
    sessions: dict[str, set[int]] = defaultdict(set)
    last_us: dict[str, int] = {}

    for cell in cells:
        dt = None if cell.hour_bucket is None else hour_bucket_dt(cell.hour_bucket)
        day_key = get_day(dt, DAY_START_HOURS).isoformat() if dt else UNKNOWN_TIME_KEY
        if not day_in_range(day_key, from_iso, until_iso):
            continue

        hour_key = HourKey(dt.weekday(), dt.hour) if dt is not None else HourKey(None, None)
        model = _display_model(cell, pricing)
        bucket = pricing.bucket_from_usage(
            usage_from_cell(cell),
            msgs=cell.requests,
            unrecorded=cell.requests if cell.usage_source == UNRECOVERABLE_SOURCE else 0,
        )
        _accumulate(by_hash_day[cell.project_hash], (day_key, hour_key, model), bucket, pricing)
        _accumulate(
            by_hash_source[cell.project_hash],
            (cell.usage_source, hour_key, model),
            bucket,
            pricing,
        )

        sessions[cell.project_hash].add(cell.session_id)
        if cell.last_started_at_us is not None:
            prior = last_us.get(cell.project_hash)
            if prior is None or cell.last_started_at_us > prior:
                last_us[cell.project_hash] = cell.last_started_at_us

    return {
        project_hash: HashUsage(
            sessions=len(sessions[project_hash]),
            last_ts=_micros_to_dt(last_us.get(project_hash)),
            by_day=price_buckets(
                by_hash_day[project_hash], pricing, refresh_pricing_data=refresh_pricing_data
            ),
            by_source=price_buckets(
                by_hash_source[project_hash], pricing, refresh_pricing_data=refresh_pricing_data
            ),
        )
        for project_hash in by_hash_day
    }


def _display_model(cell: UsageCell, pricing: PricingService) -> str:
    """
    Return the ``provider/model`` key a cell renders under.

    The provider comes from the session's own directory name, which is the sidecar's
    name (``litellm-bedrock``) rather than the upstream vendor's, and the model is
    normalized so two spellings of one model collapse to a single row.
    """
    clean = cell.model.rsplit("/", 1)[-1]
    return f"{cell.provider}/{pricing.normalize_model(clean) or clean}"


def _accumulate(
    buckets: HourBuckets,
    address: tuple[str, HourKey, str],
    bucket: Bucket,
    pricing: PricingService,
) -> None:
    """
    Merge *bucket* into ``buckets[outer][hour][model]``, creating the path as needed.

    The three keys travel as one tuple because they are one address, and because the
    caller has two of them to spell out per cell -- the day breakdown and the usage
    source breakdown differ only in their outer key.
    """
    outer_key, hour_key, model = address
    buckets.setdefault(outer_key, {}).setdefault(hour_key, {}).setdefault(
        model, pricing.new_bucket()
    ).merge(bucket)


def _micros_to_dt(value: int | None) -> datetime | None:
    """Convert an epoch-microseconds instant to a UTC datetime, or None."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC)


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
