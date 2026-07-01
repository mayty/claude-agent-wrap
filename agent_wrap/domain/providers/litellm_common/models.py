# This file has been edited with the assistance of an AI tool.
"""Data models for the LiteLLM common provider package."""

from __future__ import annotations

from typing import Any, TypedDict


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
