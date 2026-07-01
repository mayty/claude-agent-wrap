# This file has been created with the assistance of an AI tool.
"""Data models for the logs domain."""

from __future__ import annotations

from typing import Any, NamedTuple


class SessionMeta:
    """Accumulates cheap per-session metadata as records are scanned."""

    def __init__(self) -> None:
        self.count = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.models: set[str] = set()
        self.derived_alias: str | None = None
        self.derived_title: str | None = None


class ExtractedFields(NamedTuple):
    """Fields extracted from one raw or resolved log record."""

    data: dict[str, Any]
    agent_id: str | None
    reply: dict[str, Any]
    usage: dict[str, Any]
