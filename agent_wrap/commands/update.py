# This file has been created with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _is_truthy_skip(value: str) -> bool:
    return value.lower() not in ("", "0", "false", "no")


def _git(*args: str, cwd: str | None = None, timeout: int | None = None) -> tuple[str, int]:
    """Run a git command and return (stdout, returncode)."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return result.stdout.strip(), result.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", 1


def check(tool_dir: Path) -> bool:
    """Check for upstream updates. Returns True if an update was applied.

    Best-effort: any error (no network, detached HEAD, non-git dir, fetch
    failure) returns False so the caller's original command runs.
    """
    env_val = os.environ.get("CLAUDE_AGENT_SKIP_UPDATE_CHECK", "")
    if _is_truthy_skip(env_val):
        return False

    # Verify it's a git repo
    _, rc = _git("rev-parse", "--git-dir", cwd=str(tool_dir))
    if rc != 0:
        return False

    # Get current branch
    branch, rc = _git("symbolic-ref", "--short", "HEAD", cwd=str(tool_dir))
    if rc != 0 or not branch:
        return False

    # Fetch from origin (10s timeout)
    _, rc = _git("fetch", "--quiet", "origin", branch, cwd=str(tool_dir), timeout=10)
    if rc != 0:
        return False

    # Check if behind
    behind, rc = _git("rev-list", "--count", f"HEAD..origin/{branch}", cwd=str(tool_dir))
    if rc != 0 or not behind or behind == "0":
        return False

    print(f"\033[1;33mNote:\033[0m agent-wrap is {behind} commit(s) behind origin/{branch}.")
    try:
        ans = input("Update agent-wrap now? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False

    if ans.lower() != "y":
        return False

    apply(tool_dir)
    return True


def apply(tool_dir: Path) -> int:
    """Pull updates and handle default-CLAUDE.md propagation.

    Returns 0 on success, 1 on failure.
    """
    branch, rc = _git("symbolic-ref", "--short", "HEAD", cwd=str(tool_dir))
    if rc != 0:
        print("Error: could not determine current branch", file=sys.stderr)
        return 1

    before, rc = _git("rev-parse", "HEAD", cwd=str(tool_dir))
    if rc != 0:
        print("Error: could not get current HEAD", file=sys.stderr)
        return 1

    # Check default-CLAUDE.md state before pull
    user_claude_md = tool_dir / ".claude_config" / ".claude" / "CLAUDE.md"
    default_claude_md = tool_dir / "default-CLAUDE.md"
    pre_state = "missing"
    if user_claude_md.exists() and default_claude_md.exists():
        result = subprocess.run(
            ["diff", "-wB", "--strip-trailing-cr", "-q",
             str(user_claude_md), str(default_claude_md)],
            capture_output=True,
        )
        pre_state = "matches" if result.returncode == 0 else "customized"

    # Pull
    _, rc = _git("pull", "--ff-only", "origin", branch, cwd=str(tool_dir))
    if rc != 0:
        print("Error: git pull failed", file=sys.stderr)
        return 1

    after, rc = _git("rev-parse", "HEAD", cwd=str(tool_dir))
    if rc != 0:
        return 1

    print()
    if before == after:
        print("\033[1;33mNote:\033[0m already up to date; no action needed.")
        return 0

    print("\033[1;33mNote:\033[0m re-source agent-wrap.bashrc to pick up script changes.")
    print("\033[1;33mNote:\033[0m run 'rebuild_agent' to rebuild the Docker image with the updated files.")

    # Check if default-CLAUDE.md changed
    _, rc = _git("diff", "--quiet", before, after, "--", "default-CLAUDE.md", cwd=str(tool_dir))
    if rc == 0:
        return 0  # No change to default-CLAUDE.md

    if pre_state == "matches":
        user_claude_md.unlink(missing_ok=True)
        print(
            "\033[1;33mNote:\033[0m default-CLAUDE.md changed and your "
            ".claude_config/.claude/CLAUDE.md was unmodified; removed it so the "
            "next 'agent' run will install the new default."
        )
    elif pre_state == "customized":
        print()
        print(
            "\033[1;33mWarning:\033[0m default-CLAUDE.md changed upstream, but your "
            ".claude_config/.claude/CLAUDE.md has local customizations and was NOT touched."
        )
        print("To update it manually:")
        print(f'  1. Review the upstream change:  git -C "{tool_dir}" diff {before} {after} -- default-CLAUDE.md')
        print(f'  2. Compare with your copy:      diff -u "{user_claude_md}" "{default_claude_md}"')
        print(f"  3. Either merge the changes into \"{user_claude_md}\" by hand,")
        print(f"     or delete it to accept the new default (losing your customizations):")
        print(f'        rm "{user_claude_md}"')
        print("     The next 'agent' run will then copy the new default into place.")

    return 0


def run(args: list[str], tool_dir: Path) -> int:
    """Execute the `update` subcommand."""
    return apply(tool_dir)
