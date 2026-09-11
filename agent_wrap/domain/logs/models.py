# This file has been edited with the assistance of an AI tool.
"""Data models for the logs domain."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.providers.models import RequestTiming
    from agent_wrap.infrastructure.logs.models import BlobSweep, SessionKey


class DaemonState(TypedDict):
    """
    State of a logs viewer daemon that is running or coming up.

    *starting* is True between the spawn claim and the moment the viewer actually binds
    its port: the claim is written by the spawning side as soon as it knows the pid, so a
    concurrent launcher sees the slot taken instead of starting a second viewer. The
    viewer itself clears the flag once it is listening, and corrects *port* at the same
    time -- until then *port* is the port that was requested, not necessarily the one
    finally bound.
    """

    pid: int
    port: int
    starting: bool


class Fingerprint(TypedDict):
    """
    Change-marker the browser polls, and the wire shape of the ``*-stat`` endpoints.

    ``rev`` is the newest ``sessions.last_ingested_at`` in scope (unix ns, ``None`` when
    the scope holds nothing) and ``count`` is how many session rows it covers. It used to
    be a max mtime and a summed size over every ``messages.jsonl`` in scope; both halves
    now come from the index, so what the marker describes is what a fetch would return.
    """

    rev: int | None
    count: int


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
    #: True when the viewer's process is alive but has not bound its port yet, so
    #: *running* is already True while nothing is listening. See :class:`DaemonState`.
    starting: bool
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


class CombinedSessionMeta(TypedDict):
    """Per-session metadata merged across providers."""

    providers: list[str]
    session_id: str
    alias: str | None
    title: str | None
    count: int
    last_ts: float | None
    models: list[str]


class NormalizedRecordBase(TypedDict):
    """
    Core fields of one request as the viewer consumes it, before cost enrichment.

    The three request-side fields carry ``blob:<id>`` *references*, not content: the
    values behind them travel once per session rather than once per record, which is
    what keeps a session's payload proportional to its length instead of its square.
    See :mod:`agent_wrap.domain.logs.stream`. ``tools`` is ``[]`` rather than a
    reference when the request carried none, so the client's "are there any" test
    reads the same as it did when the array was inline.
    """

    timing: RequestTiming | None
    status: str | None
    model: str | None
    agent_id: str | None
    messages: list[str]
    system: str | None
    tools: list[Any] | str
    response: dict[str, Any]
    usage: dict[str, Any]
    error: str | None
    #: Why the model stopped generating, verbatim from the provider (``stop``,
    #: ``tool_calls``, ``length``, ``content_filter``, …), or None when absent.
    #: Kept unfiltered so the viewer alone decides which values it calls out.
    finish_reason: str | None
    #: The request's own ``max_tokens`` cap. Needed to read ``finish_reason ==
    #: "length"``: Claude Code's probe calls ask for a single token, so hitting the
    #: cap is the only outcome available to them and says nothing about the reply.
    max_tokens: int | None


class NormalizedRecord(NormalizedRecordBase, total=False):
    """
    One request as the viewer consumes it, priced.

    The four fields in the ``total=False`` subclass are what ``enrich_with_costs`` adds
    on top of the record's own columns and content. They are derived at read time, never
    stored: the pricing tables move, and a cost column would freeze each request at
    whatever the rates were on the day it was ingested.
    """

    context_tokens: int
    output_tokens: int
    cache_percent: int | None
    cost: float | None


class ExtractedFields(NamedTuple):
    """Fields extracted from one raw or resolved log record."""

    data: dict[str, Any]
    agent_id: str | None
    reply: dict[str, Any]
    usage: dict[str, Any]
    finish_reason: str | None


@dataclass(frozen=True)
class IngestReport:
    """
    What one ingest pass over the log tree did.

    ``sessions_seen`` counts every session directory the walk found, whether or not it
    had anything new; ``sessions_changed`` counts those that actually advanced a
    watermark. The two differing is the normal state of a warm tree — that is the point
    of the watermarks — so a report of "613 seen, 0 changed" means up to date, not idle.

    ``failed`` names the session directories that raised, one entry each, rather than
    aborting the pass. A backfill spanning hundreds of sessions should not be lost to
    one unreadable directory, but a failure must still be visible and must still make
    the verb exit non-zero, so it is collected here rather than suppressed.
    """

    sessions_seen: int
    sessions_changed: int
    sessions_reset: int
    records_ingested: int
    failed: tuple[tuple[Path, str], ...]

    @property
    def ok(self) -> bool:
        """Report whether every session the pass looked at was ingested."""
        return not self.failed


# How far behind the log files the index is, as `agent stats` reports it.
#
# Two counts rather than a list: the warning names a shortfall and points at
# `agent reindex`, and a user who wants the sessions themselves runs that. *behind* is
# the number of indexed sessions whose file has grown past its watermark, plus every
# session directory the index has never seen at all.
class IndexLag(NamedTuple):
    behind: int
    total: int

    @property
    def is_stale(self) -> bool:
        """Whether anything is missing from the index."""
        return self.behind > 0


class ExpiredSession(NamedTuple):
    """
    One session directory old enough for retention to delete, with what that costs.

    ``path`` is carried rather than rebuilt from ``key`` so that the directory the
    survey measured is the directory the run removes — the same no-re-walk discipline
    ``CleanupScope`` keeps for orphaned projects.

    ``messages_offset`` is the index's watermark, kept so the run can re-ask the one
    question that makes this safe: has the index read the whole file? A session whose
    file has grown since is not expired at all — the growth is newer than the age that
    selected it — and re-checking under the ingest lock is what turns that from a
    survey-time observation into a property of the delete.

    ``size_bytes`` is the directory's apparent size when it was surveyed, and is what
    gets reported as freed.
    """

    key: SessionKey
    path: Path
    messages_offset: int
    size_bytes: int


@dataclass(frozen=True)
class RetentionScope:
    """
    What retention would delete, surveyed before anything is removed.

    ``days`` is ``AGENT_LOGS_RETENTION_DAYS`` as it was read, and ``0`` means retention
    is switched off — which is the default, and the reason this scope is usually empty
    for a reason worth telling apart from "nothing is old enough yet".
    """

    days: int
    sessions: tuple[ExpiredSession, ...]

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing for retention to delete."""
        return not self.sessions

    @property
    def freed_estimate(self) -> int:
        """Bytes the log tree would give back, summed over the surveyed directories."""
        return sum(session.size_bytes for session in self.sessions)


class RetentionResult(NamedTuple):
    """
    What retention actually deleted: session directories, and the bytes they held.

    ``freed_bytes`` is log-tree bytes only. The index rows those sessions owned go in
    the same step, but the content behind them is reclaimed by the blob sweep that
    follows, and is reported separately because it is a different disk.
    """

    sessions: int
    freed_bytes: int


class IndexReclaim(NamedTuple):
    """
    Both halves of one reclaim pass: retention first, then the blob sweep.

    They are one result because they are one lock and one order. Retention deletes the
    session rows, which is what makes their content unreachable; the sweep is the only
    thing that then reclaims it. Run the other way round, retention would free nothing
    in the database at all until the *next* cleanup.
    """

    retention: RetentionResult
    sweep: BlobSweep
