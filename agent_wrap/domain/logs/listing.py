# This file has been created with the assistance of an AI tool.
"""
Turning indexed session rows into the lists the viewer renders.

The index stores one row per session *directory* --
``<project_hash>/<provider>/<claude_session_id>`` -- because that is what a sidecar
writes and what ingest advances a watermark against. A user sees one session, whichever
providers served it and whichever member projects of a grouped transient project it was
launched from. Collapsing the rows into that view is read-time policy, so it lives here
rather than in the schema, and it is the one thing that has to agree exactly with what
``/api/session`` later returns for the session the user opens.

Nothing here touches the filesystem: it is a pure function of the rows a caller already
has, which is what lets the viewer's whole list refresh be one query plus this.
"""

from collections import defaultdict
from operator import attrgetter
from typing import TYPE_CHECKING

from agent_wrap.domain.logs.constants import MICROSECONDS_PER_SECOND

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agent_wrap.domain.logs.models import CombinedSessionMeta, Fingerprint
    from agent_wrap.infrastructure.logs.models import SessionRow


def merge_sessions(rows: Iterable[SessionRow]) -> list[CombinedSessionMeta]:
    """
    Merge *rows* into one entry per session id, newest first.

    Rows are merged in ``SessionKey`` order so the result is reproducible: ``alias`` and
    ``title`` are whichever row first carried one, and with the input in an arbitrary
    order (which a directory walk gave, and a table scan gives) a session named under
    one provider and re-named under another could otherwise change label between two
    refreshes.

    A row with no records is dropped rather than merged. Ingest writes one for a session
    whose only new content was interned strings, and listing it would put a session in
    the sidebar that opens to nothing -- the same reason the file scan this replaces
    returned ``None`` for a session directory it found no parseable record in. Its
    *provider* is dropped with it, which is correct: nothing was served there yet.
    """
    merged: dict[str, CombinedSessionMeta] = {}
    for row in sorted(rows, key=attrgetter("key")):
        if row.record_count == 0:
            continue
        existing = merged.get(row.key.claude_session_id)
        if existing is None:
            merged[row.key.claude_session_id] = {
                "session_id": row.key.claude_session_id,
                "alias": row.alias,
                "title": row.title,
                "count": row.record_count,
                "last_ts": _seconds(row.last_event_at_us),
                "models": sorted(row.models),
                "providers": [row.key.provider],
            }
            continue
        if row.key.provider not in existing["providers"]:
            existing["providers"].append(row.key.provider)
            existing["providers"].sort()
        existing["count"] += row.record_count
        last_ts = _seconds(row.last_event_at_us)
        if last_ts is not None and (existing["last_ts"] is None or last_ts > existing["last_ts"]):
            existing["last_ts"] = last_ts
        existing["models"] = sorted(set(existing["models"]) | set(row.models))
        existing["alias"] = existing["alias"] or row.alias
        existing["title"] = existing["title"] or row.title

    out = list(merged.values())
    out.sort(key=_recency, reverse=True)
    return out


def _recency(meta: CombinedSessionMeta) -> float:
    """Sort key for the sessions list: newest first, and a session with no clock last."""
    return meta["last_ts"] or 0


def fingerprint(rows: Sequence[SessionRow]) -> Fingerprint:
    """
    Return the change-marker the browser polls for *rows*.

    ``rev`` is the newest ingest instant among them and moves whenever any of the
    sessions gains a record or has its metadata rewritten; ``count`` moves when a
    session appears or is deleted. This replaced a max-mtime / summed-size pair taken
    over every ``messages.jsonl`` the scope covered, which cost one ``stat()`` per file
    per poll and reported changes the reader could not yet see -- a write lands on disk
    before ingest has read it, so the marker moved while the served data had not.

    Rows with no records are counted here even though :func:`merge_sessions` drops them.
    The marker describes the state of the index, not of the rendered list, and a row
    that appears empty now is one whose first record is about to move ``rev`` anyway.
    """
    if not rows:
        return {"rev": None, "count": 0}
    return {"rev": max(row.last_ingested_ns for row in rows), "count": len(rows)}


def rows_by_hash(rows: Iterable[SessionRow]) -> dict[str, list[SessionRow]]:
    """
    Group *rows* by the project hash that owns them.

    The index knows a project only by its hash, and a viewer group is a list of hashes
    (a grouped transient project has one per member), so this is the join the caller
    performs between the two -- once per refresh, rather than one query per group.
    """
    grouped: dict[str, list[SessionRow]] = defaultdict(list)
    for row in rows:
        grouped[row.key.project_hash].append(row)
    return grouped


def _seconds(micros: int | None) -> float | None:
    """Convert an epoch-microseconds instant to the epoch seconds the viewer renders."""
    if micros is None:
        return None
    return micros / MICROSECONDS_PER_SECOND
