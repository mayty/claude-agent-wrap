# This file has been edited with the assistance of an AI tool.
"""Data models for the logs domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.providers.models import RequestTiming


class DaemonState(TypedDict):
    """State of a running logs viewer daemon."""

    pid: int
    port: int


class Fingerprint(TypedDict):
    """Change-marker for detecting stale caches via mtime + size."""

    mtime: int | None
    size: int | None


@dataclass(frozen=True)
class ViewerState:
    """
    A logs-viewer snapshot for reporting, including its logfile's liveness.

    Distinct from :class:`DaemonState`: that is the on-disk state file's shape, read on
    the path that also *repairs* it. This adds what a report wants (is the logfile
    growing?) and is produced without touching anything.
    """

    running: bool
    pid: int | None
    port: int | None
    #: Size of the viewer's logfile in bytes, or None when it is absent.
    log_size: int | None
    #: Epoch seconds of the logfile's last write, or None when it is absent.
    log_mtime: float | None


class GroupInfo(TypedDict):
    """A transient project group."""

    root: Path
    name: str
    paths: list[Path]
    logs_dirs: list[Path]


class ProjectInfo(TypedDict):
    """Summary row for a project in the viewer listing."""

    id: int
    path: str
    name: str
    sessions: int
    last_ts: float | None


class ProviderSessionMeta(TypedDict):
    """Per-session metadata from a single provider."""

    provider: str
    session_id: str
    alias: str | None
    title: str | None
    count: int
    first_ts: float | None
    last_ts: float | None
    models: list[str]


class CombinedSessionMeta(TypedDict):
    """Per-session metadata merged across providers."""

    providers: list[str]
    session_id: str
    alias: str | None
    title: str | None
    count: int
    first_ts: float | None
    last_ts: float | None
    models: list[str]


class NormalizedRecordBase(TypedDict):
    """Core fields of a normalized log record (before cost enrichment)."""

    timing: RequestTiming | None
    status: str | None
    model: str | None
    agent_id: str | None
    messages: list[Any]
    system: str | None
    tools: list[Any]
    response: dict[str, Any]
    usage: dict[str, Any]
    error: str | None


class NormalizedRecord(NormalizedRecordBase, total=False):
    """
    Full normalized log record after cost enrichment.

    Fields in the ``total=False`` subclass are added by ``enrich_with_costs``
    after the core fields are built by ``normalize_record``.
    """

    context_tokens: int
    output_tokens: int
    cache_percent: int | None
    cost: float | None


class ReadSessionResult(TypedDict):
    """Return type for :func:`read_session`."""

    reqs: list[NormalizedRecord]
    session_meta: CombinedSessionMeta | None


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


class ProviderSessionRead(NamedTuple):
    """Return type for :func:`_read_provider_session`."""

    records: list[NormalizedRecord]
    meta: ProviderSessionMeta | None
    strings: dict[str, str]
