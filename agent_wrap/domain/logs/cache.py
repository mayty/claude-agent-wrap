# This file has been created with the assistance of an AI tool.
"""In-memory cache and background FS watcher for the logs viewer."""

import bisect
import contextlib
import operator
import threading
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    LITELLM_LOGS_DIRNAME,
    ORPHANED_LABEL,
    PROJECT_REGISTRY_FILENAME,
)
from agent_wrap.domain.logs.constants import CACHE_POLL_INTERVAL_SEC
from agent_wrap.domain.logs.daemon import log_debug, log_info
from agent_wrap.domain.logs.io import (
    list_groups,
    list_projects,
    list_sessions,
    projects_fingerprint,
    read_session,
    read_strings,
    scan_session_meta,
    session_fingerprint,
    sessions_fingerprint,
)
from agent_wrap.domain.logs.usage_tracker import UsageTracker

if TYPE_CHECKING:
    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.logs.models import (
        CombinedSessionMeta,
        Fingerprint,
        GroupInfo,
        ProjectInfo,
    )
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.service import StatsService


class LogsCache:
    """
    In-memory cache + background FS watcher for the logs viewer.

    Populated synchronously at construction, then kept current by a daemon
    poll thread that detects new/modified/deleted ``messages.jsonl`` files
    and ``projects.txt`` changes.  The poll thread is the sole writer of
    all cached state — it builds fresh structures and atomically swaps
    references so HTTP handler threads see consistent snapshots without
    any lock.  Only the single-slot hot session cache needs a lock (both
    the poll thread and HTTP handler threads write it).
    """

    def __init__(
        self,
        stats_service: StatsService,
        config_service: ConfigService,
        pricing_service: PricingService,
    ) -> None:
        self._stats_service = stats_service
        self._pricing_service = pricing_service
        self._config = config_service
        self._hot_lock = threading.Lock()
        self._stop_event = threading.Event()

        # --- cached data (written only by poll thread) ---
        self._groups: list[GroupInfo] = []
        self._projects: list[ProjectInfo] = []
        self._projects_fp: Fingerprint = {"mtime": None, "size": None}
        self._sessions: dict[int, list[CombinedSessionMeta]] = {}
        self._sessions_fp: dict[int, Fingerprint] = {}
        self._session_fp: dict[tuple[int, str], Fingerprint] = {}

        # --- hot session cache (protected by _hot_lock) ---
        self._hot_session_key: tuple[int, str] | None = None
        self._hot_records: list[dict[str, Any]] | None = None
        self._hot_strings: str | None = None

        # --- filesystem tracking (background thread only) ---
        self._projects_txt_path = AGENT_LAUNCHES_DIR / PROJECT_REGISTRY_FILENAME
        self._projects_txt_mtime: int | None = None
        self._projects_txt_size: int | None = None
        self._known_messages: dict[Path, tuple[int, int]] = {}  # path -> (mtime_ns, size)
        self._known_project_paths: set[str] = set()

        # --- daily usage tracking ---
        self._usage_tracker = UsageTracker(pricing_service, stats_service)

        self._thread: threading.Thread | None = None

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

    def get_logs_dirs(self, project_id: int) -> list[Path] | None:
        if 0 <= project_id < len(self._groups):
            return list(self._groups[project_id]["logs_dirs"])
        return None

    # ------------------------------------------------------------------
    # Hot session cache
    # ------------------------------------------------------------------

    def get_hot_session(
        self, project_id: int, session_id: str
    ) -> tuple[list[dict[str, Any]], str] | None:
        """
        Return ``(records, strings_content)`` or ``None`` on miss.

        *records* is ``[meta_line_dict, req_0, req_1, ...]`` — the full
        session response ready for NDJSON serialization.  The caller may
        safely slice it without holding the hot lock because the list is
        never mutated in place.
        """
        with self._hot_lock:
            if (
                self._hot_session_key == (project_id, session_id)
                and self._hot_records is not None
                and self._hot_strings is not None
            ):
                return self._hot_records, self._hot_strings
            return None

    def set_hot_session(
        self,
        project_id: int,
        session_id: str,
        records: list[dict[str, Any]],
        strings_content: str,
    ) -> None:
        """
        Store *records* and *strings_content* for the given session.

        *records* should be ``[meta_line_dict, req_0, req_1, ...]`` — the
        same shape as returned by :meth:`get_hot_session`.
        """
        with self._hot_lock:
            self._hot_session_key = (project_id, session_id)
            self._hot_records = records
            self._hot_strings = strings_content

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        with log_info("Startup", "building initial session cache"):
            self._rebuild_all()
        self._thread.start()
        log_info("Startup", "background update thread started")

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            msg = "ThreadNotRunning"
            raise RuntimeError(msg)
        self._stop_event.set()
        thread.join(timeout=5.0)

    # ------------------------------------------------------------------
    # Background poller
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(CACHE_POLL_INTERVAL_SEC):
            with (
                log_debug("Update", "poll tick", threshold=timedelta(seconds=2)),
                contextlib.suppress(Exception),
            ):
                self._poll_once()

    def _poll_once(self) -> None:
        # 1. Check projects.txt for added/removed paths.
        if self._projects_txt_changed():
            with log_debug(
                "Update", "handling projects.txt change", threshold=timedelta(seconds=2)
            ):
                self._handle_projects_txt_change()

        # 2. Walk known groups' logs_dirs, stat messages.jsonl files, diff.
        with log_debug("Update", "scanning session directories", threshold=timedelta(seconds=2)):
            new_manifest, path_to_key = self._gather_directory_manifest()

        # Snapshot before incremental updates overwrite _known_messages.
        old_manifest = dict(self._known_messages)

        with log_debug("Update", "diffing manifest", threshold=timedelta(milliseconds=500)):
            changed, deleted = self._diff_manifest(new_manifest, path_to_key)

        if changed or deleted:
            with log_debug(
                "Update", "applying incremental updates", threshold=timedelta(seconds=1)
            ):
                self._apply_incremental_updates(changed, deleted, new_manifest)
        else:
            self._known_messages = new_manifest

        # 3. Update daily usage.json.
        self._update_usage_tracker(new_manifest, old_manifest)

    def _update_usage_tracker(
        self,
        new_manifest: dict[Path, tuple[int, int]],
        old_manifest: dict[Path, tuple[int, int]],
    ) -> None:
        """
        Update ``UsageTracker`` from the current manifest.

        Fingerprint comparison is owned by ``UsageTracker.update_file`` — every
        file in *new_manifest* is offered and the tracker decides whether to
        re-scan.  Deletions are detected via set difference on the manifest keys.
        Whether ``usage.json`` is rewritten or merely touched is decided by
        ``flush`` from the aggregated payload itself.
        """
        tracker = self._usage_tracker

        if tracker.detect_rollover():
            tracker.reset()

        for path, stat_info in new_manifest.items():
            tracker.update_file(path, stat_info)

        for path in set(old_manifest) - set(new_manifest):
            tracker.remove_file(path)

        tracker.flush()

    def _gather_directory_manifest(
        self,
    ) -> tuple[dict[Path, tuple[int, int]], dict[Path, tuple[int, str]]]:
        """
        Stat every messages.jsonl under known groups in a single walk.

        Returns ``(manifest, path_to_key)`` where *manifest* maps
        ``path -> (mtime_ns, size)`` and *path_to_key* maps
        ``path -> (project_id, session_id)`` — every key in *manifest* is
        guaranteed to have a matching entry in *path_to_key*.
        """
        manifest: dict[Path, tuple[int, int]] = {}
        path_to_key: dict[Path, tuple[int, str]] = {}
        for pid, group in enumerate(self._groups):
            for logs_dir in group["logs_dirs"]:
                if not logs_dir.is_dir():
                    continue
                for provider_dir in logs_dir.iterdir():
                    if not provider_dir.is_dir():
                        continue
                    for session_dir in provider_dir.iterdir():
                        if not session_dir.is_dir():
                            continue
                        mf = session_dir / "messages.jsonl"
                        if not mf.is_file():
                            continue
                        try:
                            st = mf.stat()
                        except OSError:
                            continue
                        manifest[mf] = (st.st_mtime_ns, st.st_size)
                        path_to_key[mf] = (pid, session_dir.name)
        return manifest, path_to_key

    def _diff_manifest(
        self,
        new_manifest: dict[Path, tuple[int, int]],
        path_to_key: dict[Path, tuple[int, str]],
    ) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """Compare *new_manifest* against ``_known_messages``; return (changed, deleted)."""
        changed: list[tuple[int, str]] = [
            path_to_key[mf_path]
            for mf_path, stat_info in new_manifest.items()
            if self._known_messages.get(mf_path) != stat_info
        ]

        deleted: list[tuple[int, str]] = []
        for mf_path in set(self._known_messages) - set(new_manifest):
            pid = self._resolve_deleted_project(mf_path)
            if pid is not None:
                deleted.append((pid, mf_path.parent.name))

        return changed, deleted

    # ------------------------------------------------------------------
    # Rebuild
    # ------------------------------------------------------------------

    def _rebuild_all(self) -> None:
        """Full rebuild from disk — called at startup and on projects.txt additions."""
        raw_projects = self._config.read_project_paths()

        with log_info("Rebuild", "listing groups"):
            groups = list_groups(self._stats_service, raw_projects)

        with log_info("Rebuild", "listing projects"):
            projects = list_projects(groups)

        with log_info("Rebuild", "computing projects fingerprint"):
            fp = projects_fingerprint(raw_projects)

        sessions: dict[int, list[CombinedSessionMeta]] = {}
        sessions_fp: dict[int, Fingerprint] = {}
        session_fp: dict[tuple[int, str], Fingerprint] = {}
        known_messages: dict[Path, tuple[int, int]] = {}
        with log_info("Rebuild", "scanning sessions per group"):
            for idx, group in enumerate(groups):
                sess = list_sessions(group["logs_dirs"])
                sessions[idx] = sess
                sessions_fp[idx] = sessions_fingerprint(group["logs_dirs"])
                for sm in sess:
                    key = (idx, sm["session_id"])
                    session_fp[key] = session_fingerprint(group["logs_dirs"], sm["session_id"])
                    self._record_known_messages(
                        group["logs_dirs"], sm["session_id"], known_messages
                    )

        self._track_projects_txt_state()

        self._groups = groups
        self._projects = projects
        self._projects_fp = fp
        self._sessions = sessions
        self._sessions_fp = sessions_fp
        self._session_fp = session_fp
        self._known_messages = known_messages

    def _record_known_messages(
        self,
        logs_dirs: list[Path],
        session_id: str,
        known_messages: dict[Path, tuple[int, int]],
    ) -> None:
        for logs_dir in logs_dirs:
            if not logs_dir.is_dir():
                continue
            for provider_dir in logs_dir.iterdir():
                if not provider_dir.is_dir():
                    continue
                mf = provider_dir / session_id / "messages.jsonl"
                try:
                    st = mf.stat()
                    known_messages[mf] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass

    def _track_projects_txt_state(self) -> None:
        try:
            st = self._projects_txt_path.stat()
            self._projects_txt_mtime = st.st_mtime_ns
            self._projects_txt_size = st.st_size
            self._known_project_paths = self._read_project_paths()
        except OSError:
            self._projects_txt_mtime = None
            self._projects_txt_size = None
            self._known_project_paths = set()

    # ------------------------------------------------------------------
    # Incremental updates
    # ------------------------------------------------------------------

    def _apply_incremental_updates(
        self,
        changed: list[tuple[int, str]],
        deleted: list[tuple[int, str]],
        new_manifest: dict[Path, tuple[int, int]],
    ) -> None:
        """Apply incremental session changes and refresh hot cache if needed."""
        hot_refresh_needed = False

        hot_refresh_needed |= self._apply_deletions(deleted)
        hot_refresh_needed |= self._apply_changes(changed)

        # Recompute project-level aggregates from cache.
        self._projects = self._recompute_projects_from_cache()
        self._projects_fp = self._recompute_projects_fp_from_cache()
        for pid in self._sessions:
            self._sessions_fp[pid] = self._recompute_session_fp_for_project(pid)

        self._known_messages = new_manifest

        if hot_refresh_needed:
            self._refresh_hot_cache()

    def _apply_deletions(self, deleted: list[tuple[int, str]]) -> bool:
        """Remove deleted sessions from cache.  Return True if hot cache needs refresh."""
        hot_needed = False
        for pid, sid in deleted:
            sessions = self._sessions.get(pid, [])
            self._sessions[pid] = [s for s in sessions if s["session_id"] != sid]
            self._session_fp.pop((pid, sid), None)
            if self._hot_session_key == (pid, sid):
                hot_needed = True
        return hot_needed

    def _apply_changes(self, changed: list[tuple[int, str]]) -> bool:
        """Re-scan changed sessions.  Return True if hot cache needs refresh."""
        hot_needed = False
        for pid, sid in changed:
            logs_dirs = self._groups[pid]["logs_dirs"]
            combined = self._scan_session_across_providers(logs_dirs, sid)
            self._upsert_session(pid, sid, combined)
            self._session_fp[(pid, sid)] = session_fingerprint(logs_dirs, sid)

            if self._hot_session_key == (pid, sid):
                hot_needed = True
        return hot_needed

    def _scan_session_across_providers(
        self, logs_dirs: list[Path], session_id: str
    ) -> CombinedSessionMeta | None:
        """Scan metadata for *session_id* across all provider dirs under *logs_dirs*."""
        combined: CombinedSessionMeta | None = None
        for logs_dir in logs_dirs:
            if not logs_dir.is_dir():
                continue
            for provider_dir in logs_dir.iterdir():
                if not provider_dir.is_dir():
                    continue
                session_dir = provider_dir / session_id
                if not session_dir.is_dir():
                    continue
                meta = scan_session_meta(session_dir, provider_dir.name)
                if meta is None:
                    continue
                if combined is None:
                    combined = {
                        "session_id": meta["session_id"],
                        "alias": meta["alias"],
                        "title": meta["title"],
                        "count": meta["count"],
                        "first_ts": meta["first_ts"],
                        "last_ts": meta["last_ts"],
                        "models": meta["models"],
                        "providers": [meta["provider"]],
                    }
                else:
                    self._merge_combined(combined, meta)
        return combined

    def _upsert_session(
        self, pid: int, session_id: str, combined: CombinedSessionMeta | None
    ) -> None:
        """Insert or update *combined* in ``self._sessions[pid]``, keeping newest-first sort."""
        sessions = self._sessions.get(pid, [])
        replaced = False
        for i, s in enumerate(sessions):
            if s["session_id"] == session_id:
                if combined is not None:
                    sessions[i] = combined
                else:
                    sessions.pop(i)
                replaced = True
                break
        if not replaced and combined is not None:
            sessions.append(combined)
        sessions.sort(key=lambda s: s["last_ts"] or 0, reverse=True)  # pyrefly: ignore [implicit-any-lambda]
        self._sessions[pid] = sessions

    # ------------------------------------------------------------------
    # projects.txt change handling
    # ------------------------------------------------------------------

    def _projects_txt_changed(self) -> bool:
        try:
            st = self._projects_txt_path.stat()
        except OSError:
            return self._projects_txt_mtime is not None
        else:
            return (
                st.st_mtime_ns != self._projects_txt_mtime or st.st_size != self._projects_txt_size
            )

    def _read_project_paths(self) -> set[str]:
        return {str(p) for p in self._config.read_project_paths()}

    def _handle_projects_txt_change(self) -> None:
        """Handle added/removed paths in projects.txt."""
        new_paths = self._read_project_paths()
        old_paths = self._known_project_paths
        added = new_paths - old_paths
        removed = old_paths - new_paths

        if not added and not removed:
            self._update_projects_txt_tracking()
            return

        # Process removals first (already incremental), then additions.
        if removed:
            self._prune_removed_paths(removed)

        if added:
            self._merge_added_paths(added)

        self._known_project_paths = new_paths
        self._update_projects_txt_tracking()

    def _update_projects_txt_tracking(self) -> None:
        try:
            st = self._projects_txt_path.stat()
            self._projects_txt_mtime = st.st_mtime_ns
            self._projects_txt_size = st.st_size
        except OSError:
            pass

    def _merge_added_paths(self, added: set[str]) -> None:
        """
        Incrementally merge newly added project paths into the cache.

        Processes *only* the added paths — does not iterate over all existing
        projects, so a transient project with thousands of sub-projects is not
        touched unless one of its paths appears in *added*.
        """
        old_root_to_pid: dict[Path, int] = {g["root"]: pid for pid, g in enumerate(self._groups)}

        pending_groups, merged_pids = self._classify_added_paths(added, old_root_to_pid)
        if not pending_groups and not merged_pids:
            return

        new_entries = sorted(pending_groups.values(), key=operator.itemgetter("root"))
        self._insert_new_groups(new_entries)

        # Re-index pid-keyed dicts BEFORE scanning new groups (so old data is
        # safely shifted before new data fills the vacated slots).
        new_root_to_pid: dict[Path, int] = {g["root"]: pid for pid, g in enumerate(self._groups)}
        pid_remap: dict[int, int] = {}
        for root, old_pid in old_root_to_pid.items():
            new_pid = new_root_to_pid.get(root)
            if new_pid is not None and new_pid != old_pid:
                pid_remap[old_pid] = new_pid
        if pid_remap:
            self._apply_pid_remap(pid_remap)

        # Scan sessions for new groups (now at their final pids).
        for entry in new_entries:
            pid = new_root_to_pid[entry["root"]]
            sessions = list_sessions(entry["logs_dirs"])
            self._sessions[pid] = sessions
            self._sessions_fp[pid] = sessions_fingerprint(entry["logs_dirs"])
            for sm in sessions:
                key = (pid, sm["session_id"])
                self._session_fp[key] = session_fingerprint(entry["logs_dirs"], sm["session_id"])
                self._record_known_messages(
                    entry["logs_dirs"], sm["session_id"], self._known_messages
                )

        # Refresh fingerprints for groups that had sessions merged in.
        for pid in merged_pids:
            group = self._groups[pid]
            self._sessions_fp[pid] = sessions_fingerprint(group["logs_dirs"])

        self._projects = self._recompute_projects_from_cache()
        self._projects_fp = self._recompute_projects_fp_from_cache()

    def _classify_added_paths(
        self, added: set[str], old_root_to_pid: dict[Path, int]
    ) -> tuple[dict[Path, GroupInfo], set[int]]:
        """
        Classify each added path: merge into existing group, or stage as new.

        Returns ``(pending_groups, merged_pids)`` where *pending_groups* maps
        group-root → GroupInfo for brand-new groups, and *merged_pids* is the
        set of existing group pids that had sessions merged in.
        """
        pending_groups: dict[Path, GroupInfo] = {}
        merged_pids: set[int] = set()

        for raw_path_str in added:
            try:
                path = Path(raw_path_str)
            except TypeError, ValueError:
                continue

            logs_d = self._logs_dir_for(path)
            if not logs_d.is_dir():
                continue

            group_root, display_name, _is_transient = self._stats_service.resolve_group(path)
            existing_pid = old_root_to_pid.get(group_root)

            if existing_pid is not None:
                group = self._groups[existing_pid]
                if path not in group["paths"]:
                    group["paths"].append(path)
                if logs_d not in group["logs_dirs"]:
                    group["logs_dirs"].append(logs_d)
                self._merge_sessions_from_logs_dir(existing_pid, logs_d)
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

        return pending_groups, merged_pids

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

    def _apply_pid_remap(self, pid_remap: dict[int, int]) -> None:
        """Re-key all pid-indexed caches according to *pid_remap*."""
        self._sessions = {
            pid_remap.get(old_pid, old_pid): sessions
            for old_pid, sessions in self._sessions.items()
        }
        self._sessions_fp = {
            pid_remap.get(old_pid, old_pid): fp for old_pid, fp in self._sessions_fp.items()
        }
        self._session_fp = {
            (pid_remap.get(old_pid, old_pid), sid): fp
            for (old_pid, sid), fp in self._session_fp.items()
        }
        with self._hot_lock:
            if self._hot_session_key is not None:
                hot_pid, hot_sid = self._hot_session_key
                self._hot_session_key = (
                    pid_remap.get(hot_pid, hot_pid),
                    hot_sid,
                )

    def _merge_sessions_from_logs_dir(self, pid: int, logs_dir: Path) -> None:
        """Scan sessions from *logs_dir* and merge into ``_sessions[pid]``."""
        if not logs_dir.is_dir():
            return

        sessions = self._sessions.get(pid, [])
        session_index: dict[str, int] = {s["session_id"]: i for i, s in enumerate(sessions)}
        group_logs_dirs = self._groups[pid]["logs_dirs"]

        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            provider = provider_dir.name
            for session_dir in provider_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                meta = scan_session_meta(session_dir, provider)
                if meta is None:
                    continue
                sid = meta["session_id"]
                if sid in session_index:
                    self._merge_combined(sessions[session_index[sid]], meta)
                else:
                    combined: CombinedSessionMeta = cast(
                        "CombinedSessionMeta",
                        {
                            "session_id": meta["session_id"],
                            "alias": meta["alias"],
                            "title": meta["title"],
                            "count": meta["count"],
                            "first_ts": meta["first_ts"],
                            "last_ts": meta["last_ts"],
                            "models": meta["models"],
                            "providers": [meta["provider"]],
                        },
                    )
                    sessions.append(combined)
                    session_index[sid] = len(sessions) - 1

                # Update session fingerprint and record known messages.
                self._session_fp[(pid, sid)] = session_fingerprint(group_logs_dirs, sid)
                mf = session_dir / "messages.jsonl"
                try:
                    st = mf.stat()
                    self._known_messages[mf] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass

        # Re-sort by last_ts descending.
        sessions.sort(key=lambda s: s["last_ts"] or 0, reverse=True)  # pyrefly: ignore [implicit-any-lambda]
        self._sessions[pid] = sessions

    @staticmethod
    def _resolve_path_safe(raw: str) -> Path:
        try:
            return Path(raw).resolve()
        except OSError:
            return Path(raw)

    def _prune_removed_paths(self, removed: set[str]) -> None:
        """Remove projects from cache whose paths are in *removed*."""
        removed_resolved = {self._resolve_path_safe(rp) for rp in removed}

        groups_to_drop: list[int] = []
        for pid, group in enumerate(self._groups):
            surviving = [p for p in group["paths"] if Path(p).resolve() not in removed_resolved]
            if not surviving and group["name"] != ORPHANED_LABEL:
                groups_to_drop.append(pid)
            else:
                group["paths"] = surviving
                group["logs_dirs"] = [self._logs_dir_for(p) for p in surviving]

        # Drop empty groups and re-index sessions.
        for pid in sorted(groups_to_drop, reverse=True):
            del self._groups[pid]
            self._sessions.pop(pid, None)
            self._sessions_fp.pop(pid, None)

        new_sessions: dict[int, list[CombinedSessionMeta]] = {}
        new_sessions_fp: dict[int, Fingerprint] = {}
        for old_pid in sorted(self._sessions):
            shift = sum(1 for g in groups_to_drop if g < old_pid)
            new_sessions[old_pid - shift] = self._sessions[old_pid]
            new_sessions_fp[old_pid - shift] = self._sessions_fp[old_pid]
        self._sessions = new_sessions
        self._sessions_fp = new_sessions_fp

        self._projects = self._recompute_projects_from_cache()
        self._projects_fp = self._recompute_projects_fp_from_cache()

    def _logs_dir_for(self, project_path: str | Path) -> Path:
        return Path(project_path) / ".claude" / LITELLM_LOGS_DIRNAME

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_deleted_project(self, mf_path: Path) -> int | None:
        """
        Return the project id whose logs_dirs is an ancestor of *mf_path*, or None.

        Used only for paths that no longer exist on disk (deleted between polls),
        where the fresh manifest walk can't supply the mapping. Pure path
        comparison — no filesystem I/O.
        """
        for pid, group in enumerate(self._groups):
            if any(mf_path.is_relative_to(logs_dir) for logs_dir in group["logs_dirs"]):
                return pid
        return None

    def _merge_combined(self, existing: CombinedSessionMeta, meta: Any) -> None:
        """Merge a ProviderSessionMeta-like dict into a CombinedSessionMeta in-place."""
        provider = meta.get("provider", "")
        if provider not in existing["providers"]:
            existing["providers"].append(provider)
            existing["providers"].sort()
        existing["count"] += meta.get("count", 0)
        if meta.get("first_ts") and (
            not existing["first_ts"] or meta["first_ts"] < existing["first_ts"]
        ):
            existing["first_ts"] = meta["first_ts"]
        if meta.get("last_ts") and (
            not existing["last_ts"] or meta["last_ts"] > existing["last_ts"]
        ):
            existing["last_ts"] = meta["last_ts"]
        existing["models"] = sorted(set(existing["models"]) | set(meta.get("models", [])))
        if existing.get("alias") is None and meta.get("alias") is not None:
            existing["alias"] = meta["alias"]
        if existing.get("title") is None and meta.get("title") is not None:
            existing["title"] = meta["title"]

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
        best_mtime: int | None = self._projects_txt_mtime
        total_size: int = self._projects_txt_size or 0
        for fp in self._sessions_fp.values():
            if fp["mtime"] is not None:
                if best_mtime is None or (fp["mtime"] or 0) > best_mtime:
                    best_mtime = fp["mtime"]
                total_size += fp["size"] or 0
        return {"mtime": best_mtime, "size": total_size}

    def _recompute_session_fp_for_project(self, pid: int) -> Fingerprint:
        sessions = self._sessions.get(pid, [])
        if not sessions:
            return {"mtime": None, "size": None}
        best_mtime: int | None = None
        total_size: int = 0
        for sm in sessions:
            fp = self._session_fp.get((pid, sm["session_id"]))
            if fp and fp["mtime"] is not None:
                if best_mtime is None or (fp["mtime"] or 0) > best_mtime:
                    best_mtime = fp["mtime"]
                total_size += fp["size"] or 0
        if best_mtime is None:
            return {"mtime": None, "size": None}
        return {"mtime": best_mtime, "size": total_size}

    def _refresh_hot_cache(self) -> None:
        """Re-read the currently hot session from disk and update hot cache."""
        if self._pricing_service is None:
            return
        with self._hot_lock:
            key = self._hot_session_key
        if key is None:
            return
        pid, sid = key

        logs_dirs = self.get_logs_dirs(pid)
        if logs_dirs is None:
            return

        result = read_session(logs_dirs, sid, pricing=self._pricing_service)
        strings = read_strings(logs_dirs, sid)

        meta_line: dict[str, Any] = {"__type__": "session_meta"}
        if result["session_meta"] is not None:
            meta_line.update(result["session_meta"])
        lines: list[dict[str, Any]] = cast("list[dict[str, Any]]", [meta_line, *result["reqs"]])

        with self._hot_lock:
            if self._hot_session_key == key:
                self._hot_records = lines
                self._hot_strings = strings
