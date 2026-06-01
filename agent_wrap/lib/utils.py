# This file has been created with the assistance of an AI tool.
"""Utility functions for agent-wrap."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def sanitize_name(name: str) -> str:
    """
    Normalize a string to be a valid Docker image-name suffix.

    Lowercases everything, replaces non-[a-z0-9_.-] characters with '-',
    and strips leading/trailing dashes.
    """
    lowered = name.lower()
    sanitized = re.sub(r"[^a-z0-9_.\-]", "-", lowered)
    return sanitized.strip("-")


def generate_uuid() -> str:
    """
    Generate a lowercase-hex UUID with dashes.

    Uses /proc/sys/kernel/random/uuid on Linux (cheap kernel-side entropy),
    falls back to Python's uuid4.
    """
    proc_uuid = Path("/proc/sys/kernel/random/uuid")
    if proc_uuid.is_file():
        try:
            return proc_uuid.read_text().strip()
        except OSError:
            pass
    return str(uuid.uuid4())


def is_truthy_env(value: str) -> bool:
    """Check if an env var value is truthy (not empty/0/false/no)."""
    return value.lower() not in ("", "0", "false", "no")


@dataclass
class DockerfileAgentInfo:
    """Parsed directives from a Dockerfile.agent file."""

    agent_user: str = "ubuntu"
    expose_ports: list[str] = field(default_factory=list)
    extra_run_args: list[str] = field(default_factory=list)


def parse_dockerfile_agent(dockerfile_path: Path) -> DockerfileAgentInfo:
    """
    Extract EXPOSE, agent-user, and agent-run-args directives from a Dockerfile.agent.

    Args:
        dockerfile_path: Path to the Dockerfile.agent file.

    Returns:
        DockerfileAgentInfo with parsed directives.

    """
    info = DockerfileAgentInfo()

    with open(dockerfile_path) as f:
        for raw_line in f:
            line = raw_line.strip()

            # Handle agent-user directive
            if match := re.match(r"^#\s*agent-user:\s*(\S+)", line):
                info.agent_user = match.group(1)

            # Handle agent-run-args directive
            elif match := re.match(r"^#\s*agent-run-args:\s*(.+)", line):
                # Whitespace-split into tokens (no shell quoting)
                info.extra_run_args.extend(match.group(1).split())

            # Parse EXPOSE <port> [<port>...]
            elif match := re.match(r"^[Ee][Xx][Pp][Oo][Ss][Ee]\s+(.+)", line):
                for token in match.group(1).split():
                    # Strip protocol suffix (e.g., "8080/tcp" -> "8080")
                    port = token.split("/")[0]
                    info.expose_ports.append(port)

    return info


@dataclass
class ResolvedImage:
    """Result of resolving which Docker image to use."""

    image: str
    dockerfile: Path
    context: Path


def resolve_image(tool_dir: Path, *, use_base: bool = False) -> ResolvedImage:
    """
    Determine which Docker image to use, its Dockerfile, and build context.

    Args:
        tool_dir: Path to the agent-wrap tool directory.
        use_base: If True, always use the base claude-agent image.

    Returns:
        ResolvedImage with image name, dockerfile path, and context directory.

    Raises:
        SystemExit: If Dockerfile.agent exists but is missing the required
            '# agent-name:' comment or has an invalid name.

    """
    cwd = Path.cwd()
    dockerfile_agent = cwd / "Dockerfile.agent"

    if not use_base and dockerfile_agent.is_file():
        # Extract agent-name from Dockerfile.agent
        with open(dockerfile_agent) as f:
            for line in f:
                if match := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                    name = match.group(1)
                    # Validate name format
                    if not re.match(r"^[a-z0-9_.\-]+$", name):
                        msg = (
                            f"Error: agent-name '{name}' must match [a-z0-9_.-]+ "
                            f"(Docker image names are lowercase)"
                        )
                        raise SystemExit(msg)
                    return ResolvedImage(
                        image=f"claude-agent-{name}",
                        dockerfile=dockerfile_agent,
                        context=cwd,
                    )

        # If we get here, no agent-name was found
        msg = "Error: Dockerfile.agent must contain '# agent-name: <name>' comment"
        raise SystemExit(msg)

    # Default: use base claude-agent image
    return ResolvedImage(
        image="claude-agent",
        dockerfile=tool_dir / "ops" / "Dockerfile",
        context=tool_dir,
    )
