# This file has been edited with the assistance of an AI tool.
"""Data models for the pricing domain."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class TokenUsage(NamedTuple):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cache_creation: Mapping[str, int] = MappingProxyType({})

    def cache_write_split(self) -> tuple[int, int]:
        """
        Split this record's cache-write tokens into the ``(5m, 1h)`` tiers actually charged.

        A response that reports no ephemeral breakdown -- neither in the response nor
        inferred from the request's ``cache_control`` TTL, which is the Bedrock/LiteLLM case
        -- has its flat ``cache_creation_input_tokens`` total charged wholly at the 5m rate
        as a last resort.

        Three sites have to agree on that: ``Bucket.add`` totals the token columns,
        ``CostComputer.cost_for_tiers`` prices them, and ``Provider._cost_for_tiers`` quotes
        them back in its convention-drift warning. A split that differed between them would
        put a cost in the stats table that the table's own token counts do not explain.
        Carried here, on the record itself, so the pricing and providers domains share one
        definition without either importing the other at runtime (EA001).
        """
        cw_5m = self.cache_creation.get("ephemeral_5m_input_tokens", 0)
        cw_1h = self.cache_creation.get("ephemeral_1h_input_tokens", 0)
        if cw_5m or cw_1h:
            return cw_5m, cw_1h
        return self.cache_creation_input_tokens, 0


@dataclass(slots=True)
class Bucket:
    msgs: int = 0
    in_: int = 0
    out: int = 0
    cw_5m: int = 0
    cw_1h: int = 0
    cr: int = 0
    cost: float = 0.0
    # True once any request folded in had no known price. Distinct from a `cost` of 0.0,
    # which is a *known* zero (a project whose requests all errored out was never
    # billable). Callers must branch on this flag, not on `cost <= 0.0`, to render "?".
    cost_unknown: bool = False
    # Successful requests whose usage was never recorded (response logged as a bare
    # "<Response ...>" string before the callback fix, or tagged "unrecoverable" after
    # it). They fold in as zero-token / $0 contributions, so their cost is silently
    # missing -- counted here so `agent stats` can footnote it rather than hide it.
    unrecorded: int = 0

    def add(
        self, usage: TokenUsage, request_cost: float | None = 0.0, *, unrecorded: bool = False
    ) -> None:
        self.msgs += 1
        if unrecorded:
            self.unrecorded += 1
        self.in_ += usage.input_tokens
        self.out += usage.output_tokens
        cw_5m, cw_1h = usage.cache_write_split()
        self.cw_5m += cw_5m
        self.cw_1h += cw_1h
        self.cr += usage.cache_read_input_tokens
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

    @classmethod
    def merged(cls, buckets: Iterable[Bucket]) -> Bucket:
        """
        Return a new Bucket holding the sum of *buckets*.

        Lets a consumer total a group without constructing an empty Bucket itself,
        so bucket creation stays inside the pricing domain.
        """
        total = cls()
        for bucket in buckets:
            total.merge(bucket)
        return total

    @property
    def cw(self) -> int:
        return self.cw_5m + self.cw_1h
