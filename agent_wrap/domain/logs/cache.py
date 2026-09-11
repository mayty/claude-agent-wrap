# This file has been created with the assistance of an AI tool.
"""In-memory cache and background FS watcher for the logs viewer."""

import bisect
import operator
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent_wrap.constants import (
    LITELLM_LOGS_DIRNAME,
    ORPHANED_LABEL,
    TOOL_DIR,
)
from agent_wrap.domain.logs.daemon import log_debug, log_info
from agent_wrap.domain.logs.ingest import LogFiles
from agent_wrap.domain.logs.io import list_groups
from agent_wrap.domain.logs.io import (
    logs_dir as project_logs_dir,
)
from agent_wrap.domain.logs.listing import fingerprint, merge_sessions, rows_by_hash
from agent_wrap.domain.logs.usage_tracker import UsageTracker
from agent_wrap.domain.logs.watcher import CacheWatcher

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.logs.models import (
        CombinedSessionMeta,
        Fingerprint,
        GroupInfo,
        ProjectInfo,
    )
    from agent_wrap.domain.stats.service import StatsService
    from agent_wrap.infrastructure.logs.models import Revision
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository


class LogsCache:
    """
    In-memory cache of the log tree for the logs viewer.

    Populated synchronously by :meth:`start`, then kept current by
    :class:`~agent_wrap.domain.logs.watcher.CacheWatcher`, which calls
    :meth:`apply_paths` for the ``messages.jsonl`` files a filesystem event
    named and :meth:`reconcile` for everything else.

    Both entry points do the same two things: bring the request index up to date, then
    re-derive the lists from it. A filesystem event is a *trigger*, never a source --
    nothing here parses a log file, and the only thing a path is used for is deciding
    whether the group it belongs to is already known.

    What it holds is lists and change markers, never session content: the session view
    is read off the index per request, so there is no cached copy of a session to keep
    current and nothing an HTTP handler thread writes.

    The watcher's single consumer thread is therefore the sole writer of every cached
    structure — it builds fresh ones and atomically swaps the references, so handler
    threads see consistent snapshots with no lock at all. Watchdog's own emitter threads
    never reach this class; they only put paths on the watcher's queue.
    """

    def __init__(
        self,
        stats_service: StatsService,
        config_service: ConfigService,
        session_repository: SessionRepository,
        ingest: Callable[[], object] | None = None,
    ) -> None:
        self._stats_service = stats_service
        self._config = config_service
        self._sessions_repo = session_repository
        # Called at the start of every reconcile to bring the request index up to date.
        # A callable rather than the service that owns the pass, because that service
        # owns *this* -- taking it as a dependency would be a cycle. Its return value is
        # deliberately ignored: the daemon has nowhere to report a per-session failure
        # to, and a session it could not read is one the next tick tries again. Defaults
        # to None so a test can start a cache without an index behind it.
        self._ingest = ingest

        # --- cached data (written only by the watcher's consumer thread) ---
        self._groups: list[GroupInfo] = []
        self._projects: list[ProjectInfo] = []
        self._projects_fp: Fingerprint = {"rev": None, "count": 0}
        self._sessions: dict[int, list[CombinedSessionMeta]] = {}
        self._sessions_fp: dict[int, Fingerprint] = {}
        self._session_fp: dict[tuple[int, str], Fingerprint] = {}

        # --- filesystem tracking (consumer thread only) ---
        # The tree every sidecar actually writes to. Each group's logs_dirs are
        # symlinks into it, so this is what the watcher watches -- see watcher.py.
        self._logs_tree_path = TOOL_DIR / LITELLM_LOGS_DIRNAME
        self._registry_last_change: int | None = None
        self._registry_count: int | None = None
        self._known_project_paths: set[str] = set()
        # Logs dir -> project id, so a path from a filesystem event maps to a group
        # without a scan. Registered under both spellings a logs dir can be reached by,
        # alongside the project hashes each group owns -- see _reindex_roots.
        self._root_to_pid: dict[Path, int] = {}
        self._group_hashes: list[list[str]] = []
        # The index revision the cached lists were derived from, so a heartbeat that
        # found nothing new rebuilds nothing. None means "unknown", which forces the
        # next refresh -- see _refresh_from_index.
        self._index_revision: Revision | None = None

        # --- daily usage tracking ---
        self._usage_tracker = UsageTracker(stats_service)

        self._watcher: CacheWatcher | None = None

    # ------------------------------------------------------------------
    # Public read accessors
    # ------------------------------------------------------------------

    def get_projects(self) -> list[ProjectInfo]:
        return list(self._projects)

    def get_projects_fingerprint(self) -> Fingerprint:
        return cast("Fingerprint", dict(self._projects_fp))

    def get_sessions(self, project_id: int) -> list[CombinedSessionMeta] | None:
        sessions = self._sessions.get(project_id)
        return list(sessions) if sessions is not None else None

    def get_sessions_fingerprint(self, project_id: int) -> Fingerprint | None:
        fp = self._sessions_fp.get(project_id)
        return cast("Fingerprint", dict(fp)) if fp is not None else None

    def get_session_fingerprint(self, project_id: int, session_id: str) -> Fingerprint | None:
        return self._session_fp.get((project_id, session_id))

    def get_session_meta(self, project_id: int, session_id: str) -> CombinedSessionMeta | None:
        """
        Return one session's merged summary from the cached list, or ``None``.

        What the session view's header is drawn from. Served from the list rather than
        re-derived so the header and the list entry the user clicked cannot disagree,
        and so opening a session costs no query for its own metadata.
        """
        for meta in self._sessions.get(project_id, ()):
            if meta["session_id"] == session_id:
                return meta
        return None

    def get_project_hashes(self, project_id: int) -> list[str] | None:
        """
        Return the project hashes one group owns, or ``None`` for an unknown id.

        The group's identity in the request index -- one hash per member of a grouped
        transient project, and for the ``<orphaned>`` group the central directories
        themselves. An empty list is a real answer and not the same as ``None``: a
        project whose logs dir resolves outside the shared tree owns no hash, so it has
        nothing indexed while remaining a project the viewer knows.
        """
        if 0 <= project_id < len(self._group_hashes):
            return list(self._group_hashes[project_id])
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        # Constructed here rather than in __init__ so it reads the logs tree path as it
        # stands at start time -- callers (and tests) may repoint it after construction.
        self._watcher = CacheWatcher(self, logs_tree=self._logs_tree_path)
        with log_info("Startup", "building initial session cache"):
            self.rebuild()
        self._watcher.start()
        log_info("Startup", "background update thread started")

    def stop(self) -> None:
        watcher = self._watcher
        if watcher is None:
            msg = "ThreadNotRunning"
            raise RuntimeError(msg)
        # Deliberately not cleared: stop() is called more than once on the same cache
        # (a test that stops explicitly, then a fixture that stops in its teardown), and
        # the second call must be a no-op rather than the "never started" error.
        watcher.stop()

    # ------------------------------------------------------------------
    # Update entry points (called on the watcher's consumer thread)
    # ------------------------------------------------------------------

    def apply_paths(self, paths: set[Path]) -> None:
        """
        Ingest and re-derive the lists — the filesystem-event fast path.

        Each path is a ``messages.jsonl`` a watch reported, and the only question asked
        of it is whether the group it belongs to is already known. It is not read: the
        appended records reach the cache through the index, and the refresh that follows
        is bounded by the number of *sessions* on the host rather than by what any of
        them contains, so there is nothing left for a per-path scan to save.

        Any path that cannot be mapped — the project registry, or a log dir belonging
        to no known group — falls back to :meth:`reconcile`, which re-reads the registry
        and so is the only thing that can turn that dir into a group.
        """
        # A day rollover changes which day usage.json reports, so it is reconcile's job
        # rather than this batch's. Cheap to check and true once a day.
        if self._usage_tracker.detect_rollover():
            self.reconcile()
            return

        if any(self._resolve_message_path(path) is None for path in paths):
            self.reconcile()
            return

        # Index this batch's appends before the refresh below reads the index for them.
        # Still a whole-tree pass rather than one scoped to *paths*: it is incremental,
        # so the cost of the sessions this batch did not touch is a stat each.
        self._ingest_now()
        self._refresh_from_index()
        self._usage_tracker.flush()

    def _resolve_message_path(self, path: Path) -> tuple[int, str] | None:
        """
        Map a ``messages.jsonl`` path to ``(project_id, session_id)``, or None.

        Pure path arithmetic against ``_root_to_pid`` — no filesystem access, so it
        works for a file that has just been deleted as readily as for one being
        appended to.

        What a record file is *called* is the ingester's business, so the predicate is
        asked rather than restated: nothing outside it names a log file, which is what
        ``EH001`` checks.
        """
        if not LogFiles.is_messages(path):
            return None
        session_dir = path.parent
        pid = self._root_to_pid.get(session_dir.parent.parent)
        if pid is None:
            return None
        return pid, session_dir.name

    def _reindex_roots(self) -> None:
        """
        Rebuild ``_root_to_pid`` and ``_group_hashes`` from ``_groups``.

        Each logs dir is registered under both spellings it can be reached by: as the
        group lists it -- a project's ``.claude/litellm-logs`` symlink -- and resolved,
        the real dir under the shared tree, which is what a filesystem event names. One
        mapping then answers for either spelling without scanning the group list.

        Resolving is also how a group learns its project hashes, which is the only
        identity the index stores. The hash is taken from the resolved directory's name
        *and only when its parent is the central tree*, exactly as
        ``StatsService.project_owners`` does it: a project whose symlink points somewhere
        else is disowned rather than credited with a hash that may collide with a real
        one.

        Called wherever ``_groups`` settles, since a project id is an index into it and
        every insertion shifts the ones after it.
        """
        central = self._resolve_path_safe(self._logs_tree_path)
        roots: dict[Path, int] = {}
        hashes: list[list[str]] = []
        for pid, group in enumerate(self._groups):
            group_hashes: list[str] = []
            for group_logs_dir in group["logs_dirs"]:
                resolved = self._resolve_path_safe(group_logs_dir)
                roots[group_logs_dir] = pid
                roots[resolved] = pid
                if resolved.parent == central:
                    group_hashes.append(resolved.name)
            hashes.append(group_hashes)
        self._root_to_pid = roots
        self._group_hashes = hashes

    def reconcile(self) -> None:
        """
        Re-derive every cached structure — the complete pass.

        The request index, the registry, the lists derived from the index, and the usage
        tracker. Run at startup, on the watcher's heartbeat, and whenever
        :meth:`apply_paths` sees a path it cannot attribute to a group.

        What separates it from :meth:`apply_paths` is now only the registry step: a
        project registered since the last pass has no group, so no event about its log
        dir can be attributed to one, and re-reading the registry is what creates it.
        The session lists themselves are re-derived identically by both, because both
        derive them from the whole index rather than from a walk.

        A log directory deleted out of band is no longer what this notices — the index
        is the source of the lists, so a project's sessions disappear when its rows do,
        which is ``agent cleanup``'s doing and is atomic with the deletion.
        """
        # 0. Bring the request index up to date, before anything reads from it. This is
        # the daemon's half of the arrangement that lets every consumer read the index
        # instead of the log files: nothing else on a normal host ingests, so a viewer
        # that skipped this would serve totals frozen at the last `agent reindex`.
        self._ingest_now()

        # 1. Check the registry for added/removed paths. Gated on its *contents*, not its
        # revision: a path whose logs dir does not exist yet is deliberately left out of
        # _known_project_paths, so comparing contents is what retries it on a later pass.
        # A revision gate cannot -- by then the revision has stopped moving.
        self._update_registry_tracking()
        current_paths = self._read_project_paths()
        if current_paths != self._known_project_paths:
            with log_debug("Update", "handling registry change", threshold=timedelta(seconds=2)):
                self._handle_registry_change(current_paths)

        # 2. Re-derive the project and session lists from the index.
        with log_debug("Update", "refreshing session lists", threshold=timedelta(seconds=1)):
            self._refresh_from_index()

        # 3. Update daily usage.json. Unconditional, and nothing is handed over:
        # the tracker re-aggregates today's usage out of the request index, so what
        # changed on disk tells it nothing it needs. It still runs on every pass because
        # a flush that finds no change touches usage.json, and that mtime is the
        # liveness signal the statusline reads.
        if self._usage_tracker.detect_rollover():
            self._usage_tracker.reset()
        self._usage_tracker.flush()

    def _ingest_now(self) -> None:
        """
        Run one ingest pass, if this cache was given one, and never let it stop the tick.

        Every failure mode here is transient by construction. The pass returns without
        doing anything when another process holds the ingest lock, collects a
        per-session failure rather than raising, and is idempotent — so the honest
        response to any of them is the next tick, not an aborted refresh of the
        viewer's own caches.
        """
        if self._ingest is None:
            return
        with log_debug("Update", "ingesting new requests", threshold=timedelta(seconds=2)):
            self._ingest()

    def _refresh_from_index(self) -> None:
        """
        Re-derive every list and fingerprint from the request index.

        One query for the whole ``sessions`` table, then pure Python: group the rows by
        project hash, hand each group's rows to
        :func:`~agent_wrap.domain.logs.listing.merge_sessions`, and recompute the
        fingerprints from the same rows. This is what replaced a walk of every session
        directory under every group plus a ``stat()`` of all 1,834 record files.

        Rebuilt wholesale rather than diffed. The rows are the index's own summary --
        ~600 of them on a 2.2 GB tree, one per session directory, already aggregated by
        ingest -- so there is nothing a diff could avoid that is more expensive than the
        bookkeeping it would need. The revision gate above is what keeps a quiet
        heartbeat from doing even this much.

        Nothing here touches the open session's *content*. That is read off the index per
        request, so what this publishes is only the lists and the change markers the
        browser polls -- and a marker moving is what makes the browser ask for the rest.
        """
        revision = self._sessions_repo.revision()
        if revision == self._index_revision:
            return

        grouped = rows_by_hash(self._sessions_repo.sessions())
        sessions: dict[int, list[CombinedSessionMeta]] = {}
        sessions_fp: dict[int, Fingerprint] = {}
        session_fp: dict[tuple[int, str], Fingerprint] = {}
        for pid, hashes in enumerate(self._group_hashes):
            rows = [row for project_hash in hashes for row in grouped.get(project_hash, ())]
            sessions[pid] = merge_sessions(rows)
            sessions_fp[pid] = fingerprint(rows)
            for meta in sessions[pid]:
                session_id = meta["session_id"]
                session_fp[(pid, session_id)] = fingerprint(
                    [row for row in rows if row.key.claude_session_id == session_id]
                )

        self._sessions = sessions
        self._sessions_fp = sessions_fp
        self._session_fp = session_fp
        self._projects = self._recompute_projects_from_cache()
        # After the loop: the projects fingerprint sums _sessions_fp, so computing it
        # first would publish the previous pass's value.
        self._projects_fp = self._recompute_projects_fp_from_cache()
        self._index_revision = revision

    # ------------------------------------------------------------------
    # Rebuild
    # ------------------------------------------------------------------

    def rebuild(self) -> None:
        """
        Full rebuild — the synchronous startup pass.

        Re-derives the group list, drops every cached structure, then delegates to
        :meth:`reconcile`, which refills all of them from the index. Everything a
        separate startup scan used to compute is a subset of what reconcile produces, and
        computing it twice made a project's session count mean two different things
        before and after the first update: the startup pass counted session *directories*
        (double-counting one shared by two members of a grouped transient project, and
        counting sessions whose ``messages.jsonl`` holds no parseable record), while every
        update since has counted the sessions actually cached. The table could say three
        where the drill-down rendered two.

        ``_sessions`` is pre-seeded with an empty list per group because a group whose
        sessions are not in the index yet still has to answer ``/api/sessions`` with an
        empty list rather than a 400.

        The usage tracker is still seeded here rather than left to the first update —
        reconcile ends in a flush of it. ``usage.json``'s mtime is the
        liveness signal the statusline reads, and updates are event-driven now: on a
        quiet host the first one is a heartbeat up to a minute away, and until then the
        statusline would show no totals at all on a machine that had never run a viewer.
        """
        with log_info("Rebuild", "listing groups"):
            self._groups = list_groups(self._stats_service, self._config.read_project_paths())

        # Reset explicitly rather than relying on reconcile to overwrite: its refresh is
        # gated on the index revision having moved, which on a rebuild against an
        # unchanged index would leave whatever a previous call had put here. Clearing
        # `_index_revision` is what re-opens that gate.
        self._projects = []
        self._projects_fp = {"rev": None, "count": 0}
        self._sessions = {pid: [] for pid in range(len(self._groups))}
        self._sessions_fp = {}
        self._session_fp = {}
        self._index_revision = None

        # Before reconcile, so its registry gate compares against what this just read and
        # finds nothing to do.
        self._track_registry_state()
        self._reindex_roots()

        with log_info("Rebuild", "reading the session index"):
            self.reconcile()

    def _track_registry_state(self) -> None:
        """
        Seed both the registry's contents and its fingerprint inputs.

        ``_known_project_paths`` means "registry paths already reflected in
        ``_groups``", so it is seeded with the same predicate ``list_groups`` filters on:
        a path whose ``.claude/litellm-logs`` does not exist contributes no group and is
        left out, which makes the first reconcile treat it as an addition and retry it.
        Seeding it with the whole registry instead would strand exactly the paths this
        gate exists to recover.

        Set unconditionally, including when the registry cannot be read: it is the
        left-hand side of reconcile's content gate, and leaving it stale there would fire
        a spurious pass on the very first reconcile.
        """
        self._known_project_paths = {
            raw for raw in self._read_project_paths() if project_logs_dir(Path(raw)).is_dir()
        }
        self._update_registry_tracking()

    # ------------------------------------------------------------------
    # registry change handling
    # ------------------------------------------------------------------

    def _read_project_paths(self) -> set[str]:
        return {str(p) for p in self._config.read_project_paths()}

    def _handle_registry_change(self, new_paths: set[str]) -> None:
        """
        Handle added/removed paths in the project registry.

        A path whose ``.claude/litellm-logs`` does not exist yet is deliberately *not*
        recorded as known, so the next reconcile still sees the registry as changed and
        tries it again. That keeps the content gate hot for as long as the path stays
        unresolvable, and that is the retry loop: without it, a project registered before
        its logs dir was linked stays invisible until the viewer restarts, because no
        later registry write can reintroduce it to ``added``.
        """
        old_paths = self._known_project_paths
        added = new_paths - old_paths
        removed = old_paths - new_paths

        # Process removals first (already incremental), then additions.
        if removed:
            self._prune_removed_paths(removed)

        unresolved = self._merge_added_paths(added) if added else set()

        self._known_project_paths = new_paths - unresolved

    def _update_registry_tracking(self) -> None:
        """
        Refresh the registry's revision.

        These are the only inputs to the projects fingerprint that do not come from the
        session cache, so this runs on every reconcile rather than only when the
        registry's *contents* changed: ``record_project`` refreshes the recorded project
        on every ``agent run``, moving the revision without adding or removing a path,
        and a value left stale here would be served to the browser indefinitely.
        """
        fingerprint = self._config.registry_fingerprint()
        self._registry_last_change = fingerprint.last_change
        self._registry_count = fingerprint.count

    def _merge_added_paths(self, added: set[str]) -> set[str]:
        """
        Incrementally merge newly added project paths into the group list.

        Processes *only* the added paths — does not iterate over all existing
        projects, so a transient project with thousands of sub-projects is not
        touched unless one of its paths appears in *added*.

        Only ``_groups`` is updated here. Everything keyed by project id is re-derived
        by :meth:`_refresh_from_index` immediately afterwards, which is why the gate it
        runs behind is re-opened rather than the dicts being patched: a new group changes
        which hashes belong to which pid without changing the index at all, so the
        revision it compares against would otherwise say there is nothing to do.

        Returns the subset of *added* whose logs dir does not exist yet, which the caller
        keeps out of the known set so a later pass retries it.
        """
        old_root_to_pid: dict[Path, int] = {
            group["root"]: pid for pid, group in enumerate(self._groups)
        }

        pending_groups, merged_pids, unresolved = self._classify_added_paths(added, old_root_to_pid)
        if not pending_groups and not merged_pids:
            return unresolved

        new_entries = sorted(pending_groups.values(), key=operator.itemgetter("root"))
        self._insert_new_groups(new_entries)

        self._reindex_roots()
        self._index_revision = None

        return unresolved

    def _classify_added_paths(
        self, added: set[str], old_root_to_pid: dict[Path, int]
    ) -> tuple[dict[Path, GroupInfo], set[int], set[str]]:
        """
        Classify each added path: merge into an existing group, stage as new, or defer.

        Returns ``(pending_groups, merged_pids, unresolved)`` where *pending_groups* maps
        group-root → GroupInfo for brand-new groups, *merged_pids* is the set of existing
        group pids that gained a member path, and *unresolved* holds the paths whose
        logs dir does not exist yet, for the caller to retry later.

        A path that cannot even be constructed is *not* deferred — it will never become
        valid, so retrying it forever would keep the registry gate hot for nothing.
        """
        pending_groups: dict[Path, GroupInfo] = {}
        merged_pids: set[int] = set()
        unresolved: set[str] = set()

        for raw_path_str in added:
            try:
                path = Path(raw_path_str)
            except TypeError, ValueError:
                continue

            logs_d = project_logs_dir(path)
            if not logs_d.is_dir():
                unresolved.add(raw_path_str)
                continue

            group_root, display_name, _is_transient = self._stats_service.resolve_group(path)
            existing_pid = old_root_to_pid.get(group_root)

            if existing_pid is not None:
                group = self._groups[existing_pid]
                if path not in group["paths"]:
                    group["paths"].append(path)
                if logs_d not in group["logs_dirs"]:
                    group["logs_dirs"].append(logs_d)
                merged_pids.add(existing_pid)
            elif group_root in pending_groups:
                pg = pending_groups[group_root]
                if path not in pg["paths"]:
                    pg["paths"].append(path)
                if logs_d not in pg["logs_dirs"]:
                    pg["logs_dirs"].append(logs_d)
            else:
                pending_groups[group_root] = cast(
                    "GroupInfo",
                    {
                        "root": group_root,
                        "name": display_name,
                        "paths": [path],
                        "logs_dirs": [logs_d],
                    },
                )

        return pending_groups, merged_pids, unresolved

    def _insert_new_groups(self, new_entries: list[GroupInfo]) -> None:
        """
        Insert *new_entries* into ``_groups`` at their sorted positions.

        Saves and re-appends the ``<orphaned>`` group (if present) so it stays
        at the end regardless of insertion position.
        """
        orphaned_group: GroupInfo | None = None
        if self._groups and self._groups[-1]["name"] == ORPHANED_LABEL:
            orphaned_group = self._groups.pop()

        roots = [g["root"] for g in self._groups]
        for entry in new_entries:
            idx = bisect.bisect_left(roots, entry["root"])
            self._groups.insert(idx, entry)
            roots.insert(idx, entry["root"])

        if orphaned_group is not None:
            self._groups.append(orphaned_group)

    @staticmethod
    def _resolve_path_safe(raw: str | Path) -> Path:
        try:
            return Path(raw).resolve()
        except OSError:
            return Path(raw)

    def _prune_removed_paths(self, removed: set[str]) -> None:
        """
        Drop unregistered projects from the group list.

        Groups only, like :meth:`_merge_added_paths`: every structure keyed by project id
        is rebuilt from the index by the refresh that follows, so the shifting those used
        to need is gone. Nothing else has to be carried across a group's removal —
        including the open session, whose content is read per request rather than held.
        """
        removed_resolved = {self._resolve_path_safe(rp) for rp in removed}

        groups_to_drop: list[int] = []
        for pid, group in enumerate(self._groups):
            surviving = [p for p in group["paths"] if Path(p).resolve() not in removed_resolved]
            if not surviving and group["name"] != ORPHANED_LABEL:
                groups_to_drop.append(pid)
            else:
                group["paths"] = surviving
                group["logs_dirs"] = [project_logs_dir(p) for p in surviving]

        for pid in sorted(groups_to_drop, reverse=True):
            del self._groups[pid]

        self._reindex_roots()
        self._index_revision = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _recompute_projects_from_cache(self) -> list[ProjectInfo]:
        out: list[ProjectInfo] = []
        for pid, group in enumerate(self._groups):
            session_list = self._sessions.get(pid, [])
            if not session_list:
                continue
            max_last_ts = max(
                (s["last_ts"] for s in session_list if s["last_ts"] is not None),
                default=None,
            )
            out.append(
                {
                    "id": pid,
                    "path": str(group["root"]),
                    "name": group["name"],
                    "sessions": len(session_list),
                    "last_ts": max_last_ts,
                }
            )
        out.sort(key=lambda p: p["last_ts"] or 0, reverse=True)  # pyrefly: ignore [implicit-any-lambda]
        return out

    def _recompute_projects_fp_from_cache(self) -> Fingerprint:
        """
        Combine the registry's revision with every group's into the projects marker.

        The registry belongs in it because the projects list is a join of the two: a
        project registered from a directory that already holds indexed sessions changes
        what ``/api/projects`` returns without changing a single row of the index.

        Both revisions are unix nanoseconds — ``projects.last_seen_at`` and
        ``sessions.last_ingested_at`` — so the newer of the two is meaningful, and the
        counts are summed because each moves for its own reason.
        """
        rev: int | None = self._registry_last_change
        count: int = self._registry_count or 0
        for fp in self._sessions_fp.values():
            if fp["rev"] is not None and (rev is None or fp["rev"] > rev):
                rev = fp["rev"]
            count += fp["count"]
        return {"rev": rev, "count": count}
