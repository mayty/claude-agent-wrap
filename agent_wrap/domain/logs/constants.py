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

# Today's usage totals, written by UsageTracker and read by the bundled statusline
# (as ~/.claude/usage.json inside the container — the same file via the bind mount).
# Relative to GLOBAL_CONFIG_DIR.
USAGE_JSON_RELPATH = Path(".claude") / "usage.json"

# The web UI ships as static assets under the repo-root ``logs_page/`` dir
# (server.py is at <root>/agent_wrap/cli/logs/, so the root is parents[3]).
LOGS_PAGE_DIR = Path(__file__).resolve().parents[3] / "logs_page"

# Poll interval for the in-memory cache's background filesystem watcher (seconds).
CACHE_POLL_INTERVAL_SEC = 2.0

# Compiled regexes for extracting alias names and titles from log records.
ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')
