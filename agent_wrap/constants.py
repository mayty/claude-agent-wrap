# This file has been edited with the assistance of an AI tool.
from pathlib import Path

TOOL_DIR = Path(__file__).parent.parent.resolve()
GLOBAL_CONFIG_DIR = TOOL_DIR / ".claude_config"
AGENT_LAUNCHES_DIR = TOOL_DIR / ".agent-launches"
OPS_DIR = TOOL_DIR / "ops"

# Genuine strings (not paths)
BASE_IMAGE_NAME = "claude-agent"

# Pinned sidecar Docker images (tag + digest)
LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm:v1.83.14-stable"
    "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
)
TELEGRAM_IMAGE = (
    "mayty/claude-agent-wrap-telegram:0.1.0"
    "@sha256:73c39566944046389ebd3bad89d1e4d6c2afe545f641edc74e0e08914c41d4bf"
)

# ── logs viewer ──────────────────────────────────────────────────────────────

# Env var name used by the detached logs-viewer child process to find the same
# tool_dir (and thus state file) as the parent that launched it.
LOGS_TOOL_DIR_ENV = "AGENT_LOGS_TOOL_DIR"

# Port range for the local web viewer.
LOGS_DEFAULT_PORT = 8765
LOGS_MIN_PORT = 1
LOGS_MAX_PORT = 65535

# Extension → Content-Type mapping for the static file server.
LOGS_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# ── stats ────────────────────────────────────────────────────────────────────

# Recognised usage-source tags stamped onto records by the callback.
USAGE_SOURCES = ("native", "standard_logging_object", "unrecoverable")

# Files below this count are scanned serially (fork overhead > benefit).
SCAN_PARALLEL_MIN_FILES = 64

# ── run ──────────────────────────────────────────────────────────────────────

# In-container mount point for the agent-wrap ops directory.
AGENT_WRAP_MOUNT = "/opt/agent-wrap"

# Per-project state files mounted into the agent container.
STATE_FILES = (
    "daemon.lock",
    "daemon.log",
    "daemon.status.json",
    "history.jsonl",
)
