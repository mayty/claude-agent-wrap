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
