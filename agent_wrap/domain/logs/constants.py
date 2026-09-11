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
# (this module is at <root>/agent_wrap/domain/logs/, so the root is parents[3]).
LOGS_PAGE_DIR = Path(__file__).resolve().parents[3] / "logs_page"

# Smallest response worth compressing. Below roughly a kilobyte gzip's header and
# trailer outweigh the saving, and the fingerprint endpoints -- the ones polled once a
# second -- are a few dozen bytes each, so they stay uncompressed.
GZIP_MIN_BYTES = 1024

# zlib level for viewer responses. Deliberately 1, not zlib's default of 6, because this
# server only ever talks to 127.0.0.1: loopback bandwidth is effectively free, so the
# thing worth minimising is the compression latency sitting in front of the response.
# Measured on the largest session in the tree (71.8 MB of NDJSON):
#     level 1 -> 21.7 MB (3.3x) in 0.39 s
#     level 6 -> 19.3 MB (3.7x) in 0.85 s
#     level 9 -> 19.1 MB (3.8x) in 1.89 s
# Level 1 is 2.2x faster for 11% more bytes, which is the right trade over a socket that
# moves those bytes in milliseconds. Revisit only if the viewer is ever exposed off-host.
GZIP_COMPRESS_LEVEL = 1

# The one body-bearing viewer endpoint's content type. Everything else the server
# answers is JSON, and the interned strings that used to travel as plain text now travel
# on this stream with the content that quotes them.
NDJSON_CONTENT_TYPE = "application/x-ndjson; charset=utf-8"

# The one filename that means "there is something new to ingest". The watcher forwards
# paths with this basename -- through `LogFiles.is_messages`, since EH001 keeps the name
# itself in this module and the ingester -- and drops everything else under the log tree.
# That is what keeps the fast path fast: inotify emits a directory `modified` for every
# write, and forwarding those would wake the consumer for each one. The sidecar's two
# companion files are dropped for the same reason and cost nothing by it -- the strings
# file is always written just before the record that references it, and `meta.json` is a
# cache the index replaced, so neither can be the only evidence of a change.
MESSAGES_FILENAME = "messages.jsonl"

# The sidecar's companion file, holding the originals of every string it interned. Read
# by the ingester alongside messages.jsonl and, deliberately, by nothing else.
STRINGS_FILENAME = "strings.jsonl"

# Serializes ingest host-wide. Every writer of logs.db takes this before its pass: the
# viewer daemon on a filesystem event, and `agent reindex` on demand. Taken
# non-blockingly, because two processes ingesting the same tree is not a queue to join
# -- whoever holds it is already doing the work the loser was about to do.
INGEST_LOCK_NAME = "logs-ingest.lock"

# Spinner label shown while a reindex walks the tree.
REINDEX_LABEL = "reindex"

# How old a session may get before retention deletes it, in days. Unset or 0 disables
# retention entirely, and that is the default on purpose: a session's log files are the
# only record of its spend, so an age-based default would quietly delete history a user
# never asked to lose. Read where it is used rather than at import, so that the value in
# effect is the one exported for the command actually running.
RETENTION_DAYS_ENV = "AGENT_LOGS_RETENTION_DAYS"

# Seconds in a day, for turning that number into a cutoff instant. The display domain
# keeps its own copy for formatting durations and this one cannot import it -- a runtime
# import between two domain subpackages is EA001.
SECONDS_PER_DAY = 24 * 60 * 60

# Ingest chunk bounds. Not tuning knobs: the repository writes a chunk in a single
# transaction, so ingesting the largest session (68 MB) in one chunk would build a WAL
# bigger than the session itself and lose all of it on interruption.
MAX_CHUNK_RECORDS = 200
MAX_CHUNK_PAYLOAD_BYTES = 8 * 1024 * 1024

# How the session stream refers to a blob, and the key the browser files it under. The
# `blob:` prefix is what tells the two kinds of reference apart on the client, where a
# `hash:` one is resolved from the strings table instead -- see stream.py.
BLOB_REF_FORMAT = "blob:%d"

# The two `__type__` values on the session stream. Records carry none: they are the
# stream's subject, and everything else in it is content they point at.
SESSION_META_TYPE = "session_meta"
BLOB_LINE_TYPE = "blob"

# Records per batch of the session stream. What it bounds is the content held in memory
# while a response is assembled, not the response itself: each batch's blob fetch is
# scoped to what earlier batches have not already sent, so what a batch carries is the
# *new* content of 64 records rather than everything they reference. A larger number
# means fewer round trips to SQLite for the same bytes.
SESSION_WIRE_BATCH = 64

# Log timings are epoch seconds as floats; the database stores integer microseconds so
# that ordering and windowing are exact.
MICROSECONDS_PER_SECOND = 1_000_000

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
