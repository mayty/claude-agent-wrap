# This file has been created with the assistance of an AI tool.
"""The `usage` subcommand — delegates to agent_usage.py."""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def run(args: list[str], tool_dir: Path) -> int:
    """
    Execute the `usage` subcommand.

    Delegates to agent_usage.py with the correct cache and project-registry paths.
    """
    # Import agent_usage.py from the tool directory
    usage_script = tool_dir / "agent_usage.py"
    if not usage_script.exists():
        print(f"Error: {usage_script} not found", file=sys.stderr)
        return 1

    spec = importlib.util.spec_from_file_location("agent_usage", usage_script)
    if spec is None or spec.loader is None:
        print("Error: could not load agent_usage.py", file=sys.stderr)
        return 1

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cache_path = tool_dir / ".agent-launches" / "pricing.json"
    projects_file = tool_dir / ".agent-launches" / "projects.txt"

    argv = ["agent_usage", "--cache", str(cache_path), str(projects_file), *args]
    return module.main(argv)
