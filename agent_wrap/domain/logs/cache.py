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

    A filesystem event is a *trigger*, never a source: nothing here parses a log file,
    and both entry points bring the request index up to date and then re-derive the
    lists from it.

    The watcher's single consumer thread is the sole writer of every cached structure --
    it builds fresh ones and atomically swaps the references, so handler threads see
    consistent snapshots with no lock at all. Watchdog's emitter threads never reach this
    class; they only put paths on the watcher's queue.
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
        # A callable rather than the service that owns the ingest pass: that service
        # owns *this*, so taking it as a dependency would be a cycle. The return value is
        # ignored -- the daemon has nowhere to report a per-session failure to, and the
        # next tick retries. None lets a test start a cache with no index behind it.
        self._ingest = ingest

        self._groups: list[GroupInfo] = []
        self._projects: list[ProjectInfo] = []
        self._projects_fp: Fingerprint = {"rev": None, "count": 0}
        self._sessions: dict[int, list[CombinedSessionMeta]] = {}
        self._sessions_fp: dict[int, Fingerprint] = {}
        self._session_fp: dict[tuple[int, str], Fingerprint] = {}

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

        self._usage_tracker = UsageTracker(stats_service)

        self._watcher: CacheWatcher | None = None

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

        Served from the list rather than re-derived, so the session header and the list
        entry the user clicked cannot disagree.
        """
        for meta in self._sessions.get(project_id, ()):
            if meta["session_id"] == session_id:
                return meta
        return None

    def get_project_hashes(self, project_id: int) -> list[str] | None:
        """
        Return the project hashes one group owns, or ``None`` for an unknown id.

        An empty list is a real answer and not the same as ``None``: a project whose logs
        dir resolves outside the shared tree owns no hash, so it has nothing indexed
        while remaining a project the viewer knows.
        """
        if 0 <= project_id < len(self._group_hashes):
            return list(self._group_hashes[project_id])
        return None

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

    def apply_paths(self, paths: set[Path]) -> None:
        """
        Ingest and re-derive the lists — the filesystem-event fast path.

        A reported path is never read: the only question asked of it is whether its group
        is already known. Any path that cannot be mapped falls back to
        :meth:`reconcile`, which re-reads the registry and is the only thing that can
        turn an unknown log dir into a group.
        """
        # A day rollover changes which day usage.json reports, so it is reconcile's job
        # rather than this batch's. Cheap to check and true once a day.
        if self._usage_tracker.detect_rollover():
            self.reconcile()
            return

        if any(self._resolve_message_path(path) is None for path in paths):
            self.reconcile()
            return

        # A whole-tree pass rather than one scoped to *paths*: it is incremental, so
        # the sessions this batch did not touch cost a stat each.
        self._ingest_now()
        self._refresh_from_index()
        self._usage_tracker.flush()

    def _resolve_message_path(self, path: Path) -> tuple[int, str] | None:
        """
        Map a ``messages.jsonl`` path to ``(project_id, session_id)``, or None.

        Pure path arithmetic -- no filesystem access, so a just-deleted file maps as
        readily as one being appended to. What a record file is *called* is the
        ingester's business, so the predicate is asked rather than restated (``EH001``).
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

        Each logs dir is registered under both spellings it can be reached by -- the
        project's ``.claude/litellm-logs`` symlink, and the resolved dir under the shared
        tree that a filesystem event names -- so one mapping answers for either.

        The project hash is taken from the resolved directory's name *and only when its
        parent is the central tree*, as ``StatsService.project_owners`` does it: a symlink
        pointing elsewhere is disowned rather than credited with a hash that may collide.

        Called wherever ``_groups`` settles, since a project id is an index into it.
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

        What separates it from :meth:`apply_paths` is the registry step: a project
        registered since the last pass has no group, so no event about its log dir can be
        attributed to one, and re-reading the registry is what creates it.

        The index is the source of the lists, so a project's sessions disappear when its
        rows do -- ``agent cleanup``'s doing, atomic with the directory deletion. A log
        directory removed out of band is not noticed here.
        """
        # Nothing else on a normal host ingests, so a viewer that skipped this would
        # serve totals frozen at the last `agent reindex`.
        self._ingest_now()

        # Gated on the registry's *contents*, not its revision: a path whose logs dir
        # does not exist yet is left out of _known_project_paths, and comparing contents
        # is what retries it. A revision gate cannot -- by then it has stopped moving.
        self._update_registry_tracking()
        current_paths = self._read_project_paths()
        if current_paths != self._known_project_paths:
            with log_debug("Update", "handling registry change", threshold=timedelta(seconds=2)):
                self._handle_registry_change(current_paths)

        with log_debug("Update", "refreshing session lists", threshold=timedelta(seconds=1)):
            self._refresh_from_index()

        # Unconditional: a flush that finds no change still touches usage.json, and
        # that mtime is the liveness signal the statusline reads.
        if self._usage_tracker.detect_rollover():
            self._usage_tracker.reset()
        self._usage_tracker.flush()

    def _ingest_now(self) -> None:
        """
        Run one ingest pass, if this cache was given one, and never let it stop the tick.

        Every failure mode is transient by construction: the pass no-ops when another
        process holds the ingest lock, collects per-session failures rather than raising,
        and is idempotent. The honest response to any of them is the next tick.
        """
        if self._ingest is None:
            return
        with log_debug("Update", "ingesting new requests", threshold=timedelta(seconds=2)):
            self._ingest()

    def _refresh_from_index(self) -> None:
        """
        Re-derive every list and fingerprint from the request index.

        One query for the whole ``sessions`` table, then pure Python. Rebuilt wholesale
        rather than diffed: the rows are the index's own summary -- ~600 on a 2.2 GB
        tree, one per session directory -- so a diff would cost more bookkeeping than it
        saves. The revision gate above keeps a quiet heartbeat from doing even this much.
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

    def rebuild(self) -> None:
        """
        Full rebuild — the synchronous startup pass.

        Delegates to :meth:`reconcile` rather than running a startup scan of its own: two
        implementations would each count a project's sessions their own way.

        ``_sessions`` is pre-seeded with an empty list per group, so a group whose
        sessions are not in the index yet answers ``/api/sessions`` with an empty list
        rather than a 400.

        The usage tracker is seeded here rather than at the first update because
        ``usage.json``'s mtime is the liveness signal the statusline reads: updates are
        event-driven, so on a quiet host the first is a heartbeat up to a minute away.
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

        ``_known_project_paths`` means "registry paths already reflected in ``_groups``",
        so it is seeded with the predicate ``list_groups`` filters on: a path whose
        ``.claude/litellm-logs`` does not exist is left out, which makes the first
        reconcile treat it as an addition and retry it. Seeding the whole registry
        instead would strand exactly the paths this gate exists to recover.

        Set unconditionally, including when the registry cannot be read: it is the
        left-hand side of reconcile's content gate.
        """
        self._known_project_paths = {
            raw for raw in self._read_project_paths() if project_logs_dir(Path(raw)).is_dir()
        }
        self._update_registry_tracking()

    def _read_project_paths(self) -> set[str]:
        return {str(p) for p in self._config.read_project_paths()}

    def _handle_registry_change(self, new_paths: set[str]) -> None:
        """
        Handle added/removed paths in the project registry.

        A path whose ``.claude/litellm-logs`` does not exist yet is deliberately *not*
        recorded as known, which keeps the content gate hot and retries it next pass.
        Without that, a project registered before its logs dir was linked would stay
        invisible until the viewer restarts.
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

        Runs on every reconcile, not only when the registry's *contents* changed:
        ``record_project`` moves the revision on every ``agent run`` without adding or
        removing a path, and a value left stale here would be served indefinitely.
        """
        fingerprint = self._config.registry_fingerprint()
        self._registry_last_change = fingerprint.last_change
        self._registry_count = fingerprint.count

    def _merge_added_paths(self, added: set[str]) -> set[str]:
        """
        Incrementally merge newly added project paths into the group list.

        Only ``_groups`` is updated; everything keyed by project id is re-derived by
        :meth:`_refresh_from_index` straight after. That gate is re-opened rather than
        the dicts patched, because a new group changes which hashes belong to which pid
        without changing the index -- so the revision alone would say there is nothing
        to do.

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

        A path that cannot even be constructed is *not* deferred -- it will never become
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

        Groups only, like :meth:`_merge_added_paths`: everything keyed by project id is
        rebuilt from the index by the refresh that follows.
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
        what ``/api/projects`` returns without changing a row of the index. Both
        revisions are unix nanoseconds, so the newer of the two is meaningful.
        """
        rev: int | None = self._registry_last_change
        count: int = self._registry_count or 0
        for fp in self._sessions_fp.values():
            if fp["rev"] is not None and (rev is None or fp["rev"] > rev):
                rev = fp["rev"]
            count += fp["count"]
        return {"rev": rev, "count": count}
