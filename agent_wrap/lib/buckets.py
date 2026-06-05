# This file has been created with the assistance of an AI tool.
"""The `Bucket` accumulator shared by the usage-stats subcommands."""

from __future__ import annotations


class Bucket:
    __slots__ = ("cost", "cr", "cw_1h", "cw_5m", "in_", "msgs", "out")

    def __init__(self) -> None:
        self.msgs = 0
        self.in_ = 0
        self.out = 0
        self.cw_5m = 0
        self.cw_1h = 0
        self.cr = 0
        self.cost = 0.0

    def add(self, usage: dict, request_cost: float = 0.0) -> None:
        self.msgs += 1
        self.in_ += usage.get("input_tokens", 0) or 0
        self.out += usage.get("output_tokens", 0) or 0
        cc = usage.get("cache_creation") or {}
        h5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        h1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if h5 or h1:
            self.cw_5m += h5
            self.cw_1h += h1
        else:
            # Older sessions / non-Anthropic providers may only set the flat
            # `cache_creation_input_tokens` total. Charge those at the 5m rate.
            self.cw_5m += usage.get("cache_creation_input_tokens", 0) or 0
        self.cr += usage.get("cache_read_input_tokens", 0) or 0
        self.cost += request_cost

    def merge(self, other: Bucket) -> None:
        self.msgs += other.msgs
        self.in_ += other.in_
        self.out += other.out
        self.cw_5m += other.cw_5m
        self.cw_1h += other.cw_1h
        self.cr += other.cr
        self.cost += other.cost

    @property
    def cw(self) -> int:
        return self.cw_5m + self.cw_1h

    def usage_dict(self) -> dict:
        return {
            "in": self.in_,
            "out": self.out,
            "cw_5m": self.cw_5m,
            "cw_1h": self.cw_1h,
            "cr": self.cr,
        }
