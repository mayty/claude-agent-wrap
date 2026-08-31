# This file has been edited with the assistance of an AI tool.
"""Data models for the providers domain."""

from typing import Any, TypedDict


class Tier(TypedDict):
    """
    A single pricing tier with per-unit costs in USD per 1M tokens.

    The ``in_`` field (rather than ``in``) is required because ``in`` is a
    Python keyword and cannot be used as a class attribute name.
    """

    # "in" is a Python keyword, so this field uses the in_ convention
    in_: float
    out: float
    cw_5m: float
    cw_1h: float
    cr: float
    max_in: float


class RequestTiming(TypedDict):
    start: float | None
    completionStart: float | None
    end: float | None


class MetaData(TypedDict):
    count: int
    last_ts: float | None
    models: list[str]
    alias: str | None
    title: str | None


class LogRecord(TypedDict):
    timing: RequestTiming
    status: str
    model: str
    request: dict[str, Any]
    response: dict[str, Any]
    error: str | None
