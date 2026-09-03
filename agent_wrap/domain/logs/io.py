# This file has been created with the assistance of an AI tool.
"""Filesystem I/O for log and session data."""

import contextlib
import json
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.constants import (
    LITELLM_LOGS_DIRNAME,
    ORPHANED_LABEL,
)
from agent_wrap.domain.logs.constants import MESSAGES_FILENAME
from agent_wrap.domain.logs.hash_resolver import load_strings
from agent_wrap.domain.logs.models import (
    CombinedSessionMeta,
    Fingerprint,
    GroupInfo,
    NormalizedRecord,
    ProviderSessionMeta,
    ProviderSessionRead,
    ReadSessionResult,
    SessionMeta,
)
from agent_wrap.domain.logs.normalize import (
    enrich_with_costs,
    extract_alias,
    extract_title,
    normalize_record_unresolved,
)
from agent_wrap.lib.atomic import atomic_write_json

if TYPE_CHECKING:
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.providers.models import LogRecord, MetaData
    from agent_wrap.domain.stats.service import StatsService


def logs_dir(project: Path) -> Path:
    return project / ".claude" / LITELLM_LOGS_DIRNAME


def list_groups(stats_service: StatsService, projects: list[Path]) -> list[GroupInfo]:
    """
    Group registered projects into transient projects by ``.agent_stats_leaf``.

    Returns one dict per group, ordered deterministically by group-root path so
    that a group's index is a stable id across requests. Each entry carries:

    * ``root`` — the group root :class:`~pathlib.Path` (marker dir, or the
      project itself when unmarked),
    * ``name`` — the group root's directory name,
    * ``paths`` — every member project :class:`~pathlib.Path` in the group,
    * ``logs_dirs`` — the LiteLLM logs dirs to scan for the group.

    Projects without a ``.claude/litellm-logs`` directory are skipped, mirroring
    the pre-grouping behaviour. Members are kept in registry order. A synthetic
    ``<orphaned>`` group is appended last (when present) for central log dirs left
    behind by deleted projects / stale registry entries — its ``logs_dirs`` are the
    central ``<hash>`` dirs themselves and it has no member ``paths``.
    """
    if not projects:
        return []
    names: dict[Path, str] = {}
    members: dict[Path, list[Path]] = {}
    for path in projects:
        if not logs_dir(path).is_dir():
            continue
        root, name, _transient = stats_service.resolve_group(path)
        if root not in members:
            members[root] = []
            names[root] = name
        members[root].append(path)

    # Sort by group-root path so ids are stable; callers re-sort the *public*
    # list (by recency) without disturbing this id assignment.
    groups: list[GroupInfo] = [
        {
            "root": root,
            "name": names[root],
            "paths": members[root],
            "logs_dirs": [logs_dir(p) for p in members[root]],
        }
        for root in sorted(members)
    ]

    orphaned = stats_service.orphaned_log_dirs(projects)
    if orphaned:
        groups.append(
            {
                "root": Path(ORPHANED_LABEL),
                "name": ORPHANED_LABEL,
                "paths": [],
                "logs_dirs": orphaned,
            }
        )
    return groups


def _accumulate_session_meta(meta: SessionMeta, rec: LogRecord) -> None:
    """Update *meta* with timing, model, alias, and title from *rec*."""
    meta.count += 1
    timing = rec["timing"] or {}
    end = timing.get("end")
    if isinstance(end, (int, float)):
        meta.last_ts = end
    model = rec["model"]
    if isinstance(model, str):
        meta.models.add(model.rsplit("/", 1)[-1])
    alias = extract_alias(rec)
    if alias:
        meta.derived_alias = alias
    title = extract_title(rec)
    if title:
        meta.derived_title = title


def read_meta_json(session_dir: Path) -> MetaData | None:
    """
    Read ``meta.json`` if it exists and is not older than ``messages.jsonl``.

    Returns the parsed dict on success, or ``None`` when the cache is missing,
    stale, or corrupt so the caller can fall back to a full scan.
    """
    meta_file = session_dir / "meta.json"
    messages_file = session_dir / MESSAGES_FILENAME
    if not meta_file.is_file() or not messages_file.is_file():
        return None
    try:
        meta_mtime = meta_file.stat().st_mtime_ns
        msg_mtime = messages_file.stat().st_mtime_ns
    except OSError:
        return None
    # meta.json is written *after* messages.jsonl by the callback; if it is
    # older, a write was interrupted and the cache may be incomplete.
    if meta_mtime < msg_mtime:
        return None
    try:
        cached = json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return None
    # Reject a pre-timing-format cache: `last_ts` used to be an ISO string but
    # is now an epoch float. A leftover string entry (e.g. an old sidecar that
    # rewrote meta.json) would crash the float-keyed session sort, so treat it
    # as stale and force a rescan that regenerates numeric timestamps.
    last_ts = cached.get("last_ts")
    if last_ts is not None and not isinstance(last_ts, (int, float)):
        return None
    return cached


def write_meta_json(session_dir: Path, meta: MetaData) -> None:
    """Write ``meta.json`` atomically.  Best-effort; never raises."""
    with contextlib.suppress(OSError):
        atomic_write_json(session_dir / "meta.json", meta)


def scan_session_meta(session_dir: Path, provider: str) -> ProviderSessionMeta | None:
    """
    Cheap metadata for one session: count, first/last ts, models, alias.

    Checks a ``meta.json`` cache first (maintained by the LiteLLM callback);
    falls back to a full scan of ``messages.jsonl`` when the cache is missing
    or stale, and seeds the cache after a fallback scan.
    """
    # Fast path: use the callback-maintained cache when available.
    cached = read_meta_json(session_dir)
    if cached is not None:
        return cast(
            "ProviderSessionMeta",
            {
                "provider": provider,
                "session_id": session_dir.name,
                "alias": cached.get("alias"),
                "title": cached.get("title"),
                "count": cached.get("count", 0),
                "last_ts": cached.get("last_ts"),
                "models": cached.get("models") or [],
            },
        )

    # Slow path: full scan (existing behavior).
    messages_file = session_dir / MESSAGES_FILENAME
    if not messages_file.is_file():
        return None

    meta = SessionMeta()
    try:
        with messages_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                _accumulate_session_meta(meta, rec)
    except OSError:
        return None

    if meta.count == 0:
        return None

    # Seed the cache so subsequent reads hit the fast path. last_ts may be None
    # when no record carried a timing.end (e.g. an all-failure session); the
    # cache, the float sort, and _merge_session_meta all tolerate that.
    write_meta_json(
        session_dir,
        {
            "count": meta.count,
            "last_ts": meta.last_ts,
            "models": sorted(meta.models),
            "alias": meta.derived_alias,
            "title": meta.derived_title,
        },
    )

    return {
        "provider": provider,
        "session_id": session_dir.name,
        "alias": meta.derived_alias,
        "title": meta.derived_title,
        "count": meta.count,
        "last_ts": meta.last_ts,
        "models": sorted(meta.models),
    }


def _merge_session_meta(existing: CombinedSessionMeta, meta: ProviderSessionMeta) -> None:
    """Merge *meta* (from one provider) into *existing* (the combined entry)."""
    if meta["provider"] not in existing["providers"]:
        existing["providers"].append(meta["provider"])
        existing["providers"].sort()
    existing["count"] += meta["count"]
    if meta["last_ts"] and (not existing["last_ts"] or meta["last_ts"] > existing["last_ts"]):
        existing["last_ts"] = meta["last_ts"]
    existing["models"] = sorted(set(existing["models"]) | set(meta["models"]))
    if existing["alias"] is None and meta["alias"] is not None:
        existing["alias"] = meta["alias"]
    if existing["title"] is None and meta["title"] is not None:
        existing["title"] = meta["title"]


def list_sessions(logs_dirs: list[Path]) -> list[CombinedSessionMeta]:
    """
    List sessions (newest first) across every provider in *logs_dirs*.

    Sessions with the same ``session_id`` across different providers — or across
    the member projects of a grouped transient project — are merged into a single
    entry so a mid-session provider switch (or grouping) doesn't produce duplicate
    rows in the viewer.
    """
    by_session: dict[str, CombinedSessionMeta] = {}
    for logs_dir in logs_dirs:
        if not logs_dir.is_dir():
            continue
        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            for session_dir in provider_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                meta = scan_session_meta(session_dir, provider_dir.name)
                if meta is None:
                    continue
                sid = meta["session_id"]
                if sid in by_session:
                    _merge_session_meta(by_session[sid], meta)
                else:
                    combined: CombinedSessionMeta = {
                        "session_id": meta["session_id"],
                        "alias": meta["alias"],
                        "title": meta["title"],
                        "count": meta["count"],
                        "last_ts": meta["last_ts"],
                        "models": meta["models"],
                        "providers": [meta["provider"]],
                    }
                    by_session[sid] = combined

    out = list(by_session.values())
    out.sort(key=lambda s: s["last_ts"] or 0, reverse=True)  # pyrefly: ignore [implicit-any-lambda]
    return out


def session_fingerprint(logs_dirs: list[Path], session_id: str) -> Fingerprint:
    """
    Return a combined change-marker for a session across all providers.

    Returns ``{"mtime": max_mtime_ns, "size": sum_sizes}`` across every provider
    directory (and every member project of a group) that holds this session, so a new
    record from any provider changes the marker the frontend polls.  Returns
    ``{"mtime": None, "size": None}`` when no provider has the session.
    """
    best_mtime: int | None = None
    total_size: int | None = None
    found = False

    for logs_dir in logs_dirs:
        if not logs_dir.is_dir():
            continue
        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            messages_file = provider_dir / session_id / MESSAGES_FILENAME
            try:
                st = messages_file.stat()
            except OSError:
                continue
            found = True
            if best_mtime is None or st.st_mtime_ns > best_mtime:
                best_mtime = st.st_mtime_ns
            total_size = (total_size or 0) + st.st_size

    if not found:
        return {"mtime": None, "size": None}
    return {"mtime": best_mtime, "size": total_size}


def sessions_fingerprint(logs_dirs: list[Path]) -> Fingerprint:
    """
    Return a change-marker for all sessions in a project.

    Like :func:`session_fingerprint` but across every session directory (and every
    member project of a group) so the frontend's sessions-list poll can detect new
    sessions, new records, and metadata changes without re-reading every
    messages.jsonl.

    Returns ``{"mtime": max_mtime_ns, "size": sum_sizes}`` across every
    ``messages.jsonl`` under the project's logs directory.  Returns
    ``{"mtime": None, "size": None}`` when no sessions exist.
    """
    best_mtime: int | None = None
    total_size: int | None = None
    found = False

    for logs_dir in logs_dirs:
        if not logs_dir.is_dir():
            continue
        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            for session_dir in provider_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                messages_file = session_dir / MESSAGES_FILENAME
                try:
                    st = messages_file.stat()
                except OSError:
                    continue
                found = True
                if best_mtime is None or st.st_mtime_ns > best_mtime:
                    best_mtime = st.st_mtime_ns
                total_size = (total_size or 0) + st.st_size

    if not found:
        return {"mtime": None, "size": None}
    return {"mtime": best_mtime, "size": total_size}


def _read_provider_session(
    session_dir: Path,
    provider: str,
    session_id: str,
    pricing: PricingService,
) -> ProviderSessionRead:
    """
    Read and normalize records from one provider's session directory.

    Returns ``(records, meta, strings)`` where *meta* has the same
    shape as :func:`scan_session_meta` (or ``None`` when the directory has no
    records) and *strings* is the ``{hash: original}`` map loaded from the
    session's ``strings.jsonl``.
    """
    messages_file = session_dir / MESSAGES_FILENAME
    if not messages_file.is_file():
        return ProviderSessionRead([], None, {})

    # Read every raw record first so we capture a consistent snapshot of
    # messages.jsonl.  Strings are loaded *afterwards* because the callback
    # writes hashes to strings.jsonl before appending the record — loading
    # strings after records guarantees every hash we encounter is resolvable.
    meta = SessionMeta()
    raw_records: list[dict[str, Any]] = []
    try:
        with messages_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                raw_records.append(rec)
                _accumulate_session_meta(meta, rec)
    except OSError:
        pass

    if meta.count == 0:
        return ProviderSessionRead([], None, {})

    strings = load_strings(session_dir)
    records: list[NormalizedRecord] = []
    for rec in raw_records:
        raw_response = rec.get("response")
        normalized = normalize_record_unresolved(rec)  # pyrefly: ignore [bad-argument-type]
        enriched = enrich_with_costs(
            normalized, raw_response, provider, pricing, rec.get("request")
        )
        normalized.update(enriched)  # pyrefly: ignore [no-matching-overload]
        records.append(normalized)  # pyrefly: ignore [bad-argument-type]

    entry: ProviderSessionMeta = {
        "provider": provider,
        "session_id": session_id,
        "alias": meta.derived_alias,
        "title": meta.derived_title,
        "count": meta.count,
        "last_ts": meta.last_ts,
        "models": sorted(meta.models),
    }
    return ProviderSessionRead(records, entry, strings)


def read_strings(
    logs_dirs: list[Path],
    session_id: str,
) -> str:
    """
    Return the raw ``strings.jsonl`` content for *session_id* across all
    providers, concatenated.

    Each line is a JSON object ``{"hash": "<sha256>", "original": "..."}`` —
    the same format the LiteLLM callback writes to disk.  Providers without a
    ``strings.jsonl`` are silently skipped.

    Returns an empty string when no ``strings.jsonl`` files exist.
    """
    parts: list[str] = []
    for logs_dir in logs_dirs:
        if not logs_dir.is_dir():
            continue
        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            sf = provider_dir / session_id / "strings.jsonl"
            if not sf.is_file():
                continue
            try:
                parts.append(sf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "".join(parts)


def _append_order_keys(records: list[NormalizedRecord]) -> list[float]:
    """
    Chronological sort keys for one provider's records, in file order.

    A record with no ``timing.start`` inherits the last start seen instead of
    sorting as 0. Historically every failure was written with an all-null
    ``timing`` (see ``_record_failure`` in the sidecar callback), so keying on
    ``start or 0`` hoisted them to the top of the stream — the session-start quota
    probe's 429 appeared above the conversation it preceded by milliseconds, and a
    mid-session failure appeared to have happened before the session began.

    ``messages.jsonl`` is append-only, so a record's file position is its true
    position; carrying the previous start forward reproduces it. Records written
    since failures gained timestamps do not rely on this. Leading timing-less
    records keep the 0.0 key and stay first, which is where they were appended.
    """
    keys: list[float] = []
    last = 0.0
    for rec in records:
        timing = rec["timing"]
        start = timing.get("start") if timing else None
        if isinstance(start, (int, float)):
            last = float(start)
        keys.append(last)
    return keys


def read_session(
    logs_dirs: list[Path],
    session_id: str,
    pricing: PricingService,
    *,
    from_index: int = 0,
) -> ReadSessionResult:
    """
    Read and normalize every request in one session across all providers.

    When a session spans multiple providers (e.g. the user switched mid-session) —
    or multiple member projects of a grouped transient project — records from every
    provider directory are loaded and merge-sorted by ``ts`` so the chat view shows
    a single chronological thread.

    When *from_index* > 0, only records at that index and beyond are returned in
    ``reqs`` — *session_meta* is still computed from all records so the header
    always reflects the full session (count, timestamps, models).

    Returns ``{"reqs": [...], "session_meta": {...}}`` where *session_meta* has
    the same shape as one entry from :func:`list_sessions` (or ``None`` when no
    records exist).
    """
    # (sort key, record) pairs. The key comes from _append_order_keys, which needs
    # one provider's records in file order, so it is computed per provider dir and
    # the merge happens afterwards.
    keyed: list[tuple[float, NormalizedRecord]] = []
    combined_meta: CombinedSessionMeta | None = None

    for logs_dir in logs_dirs:
        if not logs_dir.is_dir():
            continue
        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            records, entry, _strings = _read_provider_session(
                provider_dir / session_id, provider_dir.name, session_id, pricing
            )
            keyed.extend(zip(_append_order_keys(records), records, strict=True))
            if entry is not None:
                if combined_meta is None:
                    combined_meta = {
                        "session_id": entry["session_id"],
                        "alias": entry["alias"],
                        "title": entry["title"],
                        "count": entry["count"],
                        "last_ts": entry["last_ts"],
                        "models": entry["models"],
                        "providers": [entry["provider"]],
                    }
                else:
                    _merge_session_meta(combined_meta, entry)

    # Stable, so records sharing a key stay in the order their provider dirs were
    # read — the same cross-provider tie-breaking the previous sort had.
    keyed.sort(key=itemgetter(0))
    all_records = [rec for _key, rec in keyed]
    return {"reqs": all_records[from_index:], "session_meta": combined_meta}
