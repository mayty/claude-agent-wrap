# This file has been created with the assistance of an AI tool.
"""Constants for the logs domain subpackage."""

import re
from pathlib import Path

# Spinner label shown during cold start.
LOGS_VIEWER_LABEL = "logs-viewer"

# Background-viewer lifecycle constants.
LOG_FILE_NAME = "logs-server.log"

# Parent -> child handshake / stop-wait timing.
SPAWN_TIMEOUT_SEC = 30.0
STOP_TIMEOUT_SEC = 3.0
POLL_INTERVAL_SEC = 0.05

# Global, gitignored runtime state for the background viewer.
STATE_FILE_NAME = "logs-server.json"

# The web UI ships as static assets under the repo-root ``logs_page/`` dir
# (server.py is at <root>/agent_wrap/cli/logs/, so the root is parents[3]).
LOGS_PAGE_DIR = Path(__file__).resolve().parents[3] / "logs_page"

# Port scan limit when binding — try up to this many ports before giving up.
PORT_SCAN_LIMIT = 50

# Poll interval for the in-memory cache's background filesystem watcher (seconds).
CACHE_POLL_INTERVAL_SEC = 2.0

# Compiled regexes for extracting alias names and titles from log records.
ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')
