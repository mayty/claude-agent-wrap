# This file has been edited with the assistance of an AI tool.
"""Constants for the logs domain subpackage."""

import os
import re
from pathlib import Path

from agent_wrap.lib.utils import is_truthy_env

# Spinner label shown during cold start.
LOGS_VIEWER_LABEL = "logs-viewer"

# Verbose per-tick/per-step server logging, opt-in via AGENT_LOG_DEBUG=1.
LOG_DEBUG = is_truthy_env(os.environ.get("AGENT_LOG_DEBUG", ""))

# Background-viewer lifecycle constants.
LOG_FILE_NAME = "logs-server.log"

# Parent -> child handshake / stop-wait timing.
SPAWN_TIMEOUT_SEC = 900.0
STOP_TIMEOUT_SEC = 8.0
POLL_INTERVAL_SEC = 0.05

# Global, gitignored runtime state for the background viewer.
STATE_FILE_NAME = "logs-server.json"

# Serializes the decide-then-spawn window so two launchers cannot both conclude that no
# viewer is running and each start one. Held across a state read, a fork and one small
# write -- never across the viewer's cold start -- so the timeout only has to cover
# scheduling noise, and exceeding it means something is wrong rather than merely busy.
SPAWN_LOCK_NAME = "logs-server.lock"
SPAWN_LOCK_TIMEOUT_SEC = 5.0

# Today's usage totals, written by UsageTracker and read by the bundled statusline
# (as ~/.claude/usage.json inside the container — the same file via the bind mount).
# Relative to GLOBAL_CONFIG_DIR.
USAGE_JSON_RELPATH = Path(".claude") / "usage.json"

# The web UI ships as static assets under the repo-root ``logs_page/`` dir
# (server.py is at <root>/agent_wrap/cli/logs/, so the root is parents[3]).
LOGS_PAGE_DIR = Path(__file__).resolve().parents[3] / "logs_page"

# The one filename the cache tracks. Every manifest entry and every Fingerprint in
# io.py is keyed off it, so the watcher enqueues paths with this basename and drops
# everything else under the log tree. That positive filter is what keeps the fast path
# fast: inotify emits a directory `modified` for every write, and forwarding those
# would degrade each batch to a full rescan. It also closes two feedback loops for
# free, since the viewer itself writes `meta.json` into the tree it is watching (see
# io.write_meta_json) and the sidecar writes `strings.jsonl` beside each messages file
# -- neither is fingerprinted, and both accompany a messages.jsonl write anyway.
MESSAGES_FILENAME = "messages.jsonl"

# How often the cache reconciles when the filesystem has been quiet. There is no
# non-watchdog *mode* to switch into, so this is not a fallback the viewer chooses; it is
# the tick for the three jobs no filesystem event can announce:
#   * usage.json's mtime is the liveness signal ops/statusline.py reads, and it treats
#     the file as stale after 30 minutes, so this sits 30x inside that budget;
#   * UsageTracker.detect_rollover() has to fire on a machine with no log activity;
#   * `agent cleanup` deletes orphaned log dirs out of band, and a rare, user-invoked
#     deletion reconciling within a minute is fine.
# It does double as a working degraded mode, though, and the docs describe it that way:
# reconcile() is a complete rescan, so where events never arrive at all -- see
# EVENTLESS_FILESYSTEMS -- the viewer still notices everything, just this late.
CACHE_HEARTBEAT_INTERVAL_SEC = 60.0

# How long stop() waits for the observer and for the consumer thread, each.
CACHE_STOP_TIMEOUT_SEC = 5.0

# Filesystem types that accept an inotify watch and then never deliver an event.
# There is no way to detect this at runtime -- inotify_add_watch succeeds and the
# queue simply stays empty -- so the wrapper's own install location is checked against
# this list instead and the user is told to move it. Only the wrapper's location
# matters: every sidecar writes into TOOL_DIR/litellm-logs, so a project on a Windows
# drive or a network share is served correctly as long as agent-wrap itself is not.
EVENTLESS_FILESYSTEMS = frozenset({"drvfs", "9p", "v9fs", "cifs", "smb3", "nfs", "nfs4"})

# Put on the watcher's queue by stop() to unblock the consumer. queue.SimpleQueue has
# no shutdown() on the pinned interpreter -- only queue.Queue gained one in 3.13 -- and
# a sentinel is both cheaper and unambiguous. Identity is the whole contract, so the
# value is a bare object() and is only ever compared with `is`.
WATCH_STOP = object()

# Compiled regexes for extracting alias names and titles from log records.
ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')
