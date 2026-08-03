# This file has been created with the assistance of an AI tool.
"""Data/type-carrying classes for the `agent stats` render layer."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from agent_wrap.domain.display.models import RowItemOrDivider
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.pricing.models import Bucket

#: A "cost view" callback. cost/unknown MUST come from this, never ``Bucket.cost``.
CostFn = Callable[[str, "Bucket"], tuple[float, bool]]
#: Renders the per-model section of a table body.
BuildModelSection = Callable[[dict[str, "Bucket"], int, "DisplayService"], list["RowItemOrDivider"]]


class AggregatedDayRows(NamedTuple):
    """Aggregated per-day rows with totals for the By-day table."""

    day_rows_data: list[tuple[str, Bucket, float, bool]]
    total_b: Bucket
    total_cost: float
    total_unknown: bool
