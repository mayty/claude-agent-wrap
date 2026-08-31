# This file has been created with the assistance of an AI tool.
"""
Persistence for the orphaned-usage archive.

``agent cleanup`` deletes the central log dirs of projects that are gone from the
registry, but their spend must keep appearing in ``agent stats``. This module
owns the derived archive that preserves it: reading, merging, and writing the
``date -> hour -> model -> source`` document described by ``ArchiveDoc``.

Kept separate from ``scan.py`` (which parses raw ``messages.jsonl``) because this
is the opposite direction — persisting an already-derived aggregate. Two
invariants matter here:

* **UTC hours, never bucketed days.** Day bucketing depends on
  ``DAY_START_HOURS``, which the user can change after a cleanup runs. Storing
  raw UTC hours lets ``agent stats`` re-derive the day at read time.
  ``DAY_START_HOURS`` is a whole number of hours, so a day boundary always falls
  on an hour boundary and every instant within one archived hour re-buckets
  identically.
* **No cost.** Pricing tables change too, so only token counts are archived and
  ``price_buckets`` is applied fresh on every read. Nothing here may call it.
"""

import json
from collections import defaultdict
from datetime import UTC
from typing import TYPE_CHECKING

from agent_wrap.domain.stats.constants import UNKNOWN_TIME_KEY
from agent_wrap.lib.atomic import atomic_write_json

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from agent_wrap.domain.pricing.models import Bucket
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.models import ArchiveDoc, ArchiveLeaf, RawRecord


def archive_time_keys(ts: datetime | None) -> tuple[str, str]:
    """
    Return the ``(date, hour)`` archive keys for *ts*, in UTC.

    Deliberately does NOT apply ``DAY_START_HOURS`` — see the module docstring.
    A ``None`` timestamp yields ``("?", "?")``.
    """
    if ts is None:
        return UNKNOWN_TIME_KEY, UNKNOWN_TIME_KEY
    utc = ts.astimezone(UTC)
    return utc.strftime("%Y-%m-%d"), utc.strftime("%H")


def fold_records_into_archive(records: list[RawRecord], pricing: PricingService) -> ArchiveDoc:
    """
    Fold raw scan records into the serializable archive shape.

    Accumulation runs through real ``Bucket``s (via ``pricing.new_bucket()``) so
    the 5m/1h cache-write tier attribution — including ``Bucket.add``'s
    flat-total fallback — stays solely in the pricing domain rather than being
    reimplemented here. Model names are normalized exactly as
    ``fold_raw_to_buckets`` does, so archived rows collapse onto live ones in the
    stats tables instead of splitting into near-duplicate keys.
    """
    buckets: dict[tuple[str, str, str, str], Bucket] = defaultdict(pricing.new_bucket)
    for rec in records:
        date_key, hour_key = archive_time_keys(rec.ts)
        provider, _, model = rec.display_model.partition("/")
        norm_display = f"{provider}/{pricing.normalize_model(model) or model}"
        buckets[(date_key, hour_key, norm_display, rec.source)].add(
            rec.usage, 0.0, unrecorded=rec.unrecorded
        )

    doc: ArchiveDoc = {}
    for (date_key, hour_key, model_key, source), b in buckets.items():
        by_source = doc.setdefault(date_key, {}).setdefault(hour_key, {}).setdefault(model_key, {})
        by_source[source] = {
            "msgs": b.msgs,
            "input_tokens": b.in_,
            "output_tokens": b.out,
            "cache_write_5m": b.cw_5m,
            "cache_write_1h": b.cw_1h,
            "cache_read": b.cr,
            "unrecorded": b.unrecorded,
        }
    return doc


class _Leaf:
    """
    Leaf-level arithmetic for archive documents.

    Every field is read with a ``0`` default so a hand-edited or
    older-schema archive missing a key merges without raising.
    """

    @staticmethod
    def copy(leaf: ArchiveLeaf) -> ArchiveLeaf:
        """Return an independent copy of *leaf*."""
        return {
            "msgs": leaf.get("msgs", 0),
            "input_tokens": leaf.get("input_tokens", 0),
            "output_tokens": leaf.get("output_tokens", 0),
            "cache_write_5m": leaf.get("cache_write_5m", 0),
            "cache_write_1h": leaf.get("cache_write_1h", 0),
            "cache_read": leaf.get("cache_read", 0),
            "unrecorded": leaf.get("unrecorded", 0),
        }

    @staticmethod
    def add(dst: ArchiveLeaf, src: ArchiveLeaf) -> None:
        """
        Add every counter in *src* into *dst* in place.

        Both sides default missing fields to 0 — *dst* may itself have come from
        an incomplete archive, so it cannot be assumed to hold every key either.
        """
        dst["msgs"] = dst.get("msgs", 0) + src.get("msgs", 0)
        dst["input_tokens"] = dst.get("input_tokens", 0) + src.get("input_tokens", 0)
        dst["output_tokens"] = dst.get("output_tokens", 0) + src.get("output_tokens", 0)
        dst["cache_write_5m"] = dst.get("cache_write_5m", 0) + src.get("cache_write_5m", 0)
        dst["cache_write_1h"] = dst.get("cache_write_1h", 0) + src.get("cache_write_1h", 0)
        dst["cache_read"] = dst.get("cache_read", 0) + src.get("cache_read", 0)
        dst["unrecorded"] = dst.get("unrecorded", 0) + src.get("unrecorded", 0)


def merge_archives(dst: ArchiveDoc, src: ArchiveDoc) -> None:
    """
    Deep-merge *src* into *dst* in place, summing overlapping leaves.

    Plain integer arithmetic — both sides are already tier-attributed, so no
    ``Bucket`` is needed. Overlapping ``(date, hour, model, source)`` cells sum
    into one leaf rather than accumulating duplicate rows, which is why the
    archive is a merged document instead of an append-only log: separate projects
    and repeated cleanup runs overlap heavily in time.
    """
    for date_key, by_hour in src.items():
        dst_by_hour = dst.setdefault(date_key, {})
        for hour_key, by_model in by_hour.items():
            dst_by_model = dst_by_hour.setdefault(hour_key, {})
            for model_key, by_source in by_model.items():
                dst_by_source = dst_by_model.setdefault(model_key, {})
                for source, leaf in by_source.items():
                    existing = dst_by_source.get(source)
                    if existing is None:
                        dst_by_source[source] = _Leaf.copy(leaf)
                    else:
                        _Leaf.add(existing, leaf)


def read_archive(path: Path) -> ArchiveDoc:
    """
    Read the usage archive at *path*, or ``{}`` when absent or unreadable.

    Best-effort by design, matching ``orphaned_log_dirs``: a corrupt or truncated
    archive must not break ``agent stats``. A non-object payload is treated as
    empty rather than trusted.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_archive(path: Path, doc: ArchiveDoc) -> None:
    """
    Atomically write *doc* to *path*, chronologically sorted at every level.

    Sorting is cheap and keeps the file diffable and greppable.
    """
    sorted_doc: ArchiveDoc = {
        date_key: {
            hour_key: {
                model_key: dict(sorted(by_source.items()))
                for model_key, by_source in sorted(by_model.items())
            }
            for hour_key, by_model in sorted(doc[date_key].items())
        }
        for date_key in sorted(doc)
    }
    atomic_write_json(path, sorted_doc)
