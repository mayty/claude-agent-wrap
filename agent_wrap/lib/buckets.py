# This file has been created with the assistance of an AI tool.
"""The `Bucket` accumulator shared by the usage-stats subcommands."""

from __future__ import annotations

from typing import Any


class Bucket:
    __slots__ = (
        "cost",
        "cost_unknown",
        "cr",
        "cw_1h",
        "cw_5m",
        "in_",
        "msgs",
        "out",
        "unrecorded",
    )

    def __init__(self) -> None:
        self.msgs = 0
        self.in_ = 0
        self.out = 0
        self.cw_5m = 0
        self.cw_1h = 0
        self.cr = 0
        self.cost = 0.0
        # True once any request folded in had no known price. This is distinct
        # from a `cost` of 0.0, which is a *known* zero (e.g. a project whose
        # requests all errored out and so were never billable). Callers must
        # use this flag — not `cost <= 0.0` — to decide whether to render "?".
        self.cost_unknown = False
        # Count of successful requests whose usage was never recorded (response
        # logged as a bare "<Response ...>" string before the callback fix, or
        # tagged "_usage_source": "unrecoverable" after it). These fold in as
        # zero-token / $0 contributions, so their cost is silently missing —
        # tracked here so `agent stats` can footnote the count rather than hide it.
        self.unrecorded = 0

    def add(
        self, usage: dict[str, Any], request_cost: float | None = 0.0, *, unrecorded: bool = False
    ) -> None:
        self.msgs += 1
        if unrecorded:
            self.unrecorded += 1
        self.in_ += usage.get("input_tokens", 0) or 0
        self.out += usage.get("output_tokens", 0) or 0
        cc = usage.get("cache_creation") or {}
        h5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        h1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if h5 or h1:
            self.cw_5m += h5
            self.cw_1h += h1
        else:
            # No ephemeral 5m/1h split was available — neither from the response
            # nor inferred from the request's cache_control TTL (see
            # stats.extract_usage / request_cache_ttl). Charge the flat
            # `cache_creation_input_tokens` total at the 5m rate as a last resort.
            self.cw_5m += usage.get("cache_creation_input_tokens", 0) or 0
        self.cr += usage.get("cache_read_input_tokens", 0) or 0
        # A None cost means pricing was unavailable for this request; track that
        # as unknown rather than silently treating it as a $0.00 contribution.
        if request_cost is None:
            self.cost_unknown = True
        else:
            self.cost += request_cost

    def merge(self, other: Bucket) -> None:
        self.msgs += other.msgs
        self.in_ += other.in_
        self.out += other.out
        self.cw_5m += other.cw_5m
        self.cw_1h += other.cw_1h
        self.cr += other.cr
        self.cost += other.cost
        self.cost_unknown = self.cost_unknown or other.cost_unknown
        self.unrecorded += other.unrecorded

    @property
    def cw(self) -> int:
        return self.cw_5m + self.cw_1h
