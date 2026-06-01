# This file has been created with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from agent_wrap.lib.console import Ansi
from agent_wrap.lib.utils import is_truthy_env

if TYPE_CHECKING:
    from pathlib import Path


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


def _get_behind_count(tool_dir: Path) -> tuple[str, int] | None:
    """Return (branch, commits_behind) if there are upstream updates, else None."""
    _, rc = _git("rev-parse", "--git-dir", cwd=str(tool_dir))
    if rc != 0:
        return None

    branch, rc = _git("symbolic-ref", "--short", "HEAD", cwd=str(tool_dir))
    if rc != 0 or not branch:
        return None

    _, rc = _git("fetch", "--quiet", "origin", branch, cwd=str(tool_dir), timeout=10)
    if rc != 0:
        return None

    behind, rc = _git("rev-list", "--count", f"HEAD..origin/{branch}", cwd=str(tool_dir))
    if rc != 0 or not behind or behind == "0":
        return None

    return branch, int(behind)


def check(tool_dir: Path) -> bool:
    """
    Check for upstream updates. Returns True if an update was applied.

    Best-effort: any error (no network, detached HEAD, non-git dir, fetch
    failure) returns False so the caller's original command runs.
    """
    env_val = os.environ.get("CLAUDE_AGENT_SKIP_UPDATE_CHECK", "")
    if is_truthy_env(env_val):
        return False

    result = _get_behind_count(tool_dir)
    if result is None:
        return False

    branch, behind = result
    print(
        f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} agent-wrap is {behind} commit(s) behind origin/{branch}."
    )
    try:
        ans = input("Update agent-wrap now? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False

    if ans.lower() != "y":
        return False

    apply(tool_dir)
    return True


def _detect_claude_md_state(tool_dir: Path) -> str:
    """Return 'matches', 'customized', or 'missing' for user's CLAUDE.md."""
    user_claude_md = tool_dir / ".claude_config" / ".claude" / "CLAUDE.md"
    default_claude_md = tool_dir / "ops" / "default-CLAUDE.md"
    if not (user_claude_md.exists() and default_claude_md.exists()):
        return "missing"
    result = subprocess.run(
        [
            "diff",
            "-wB",
            "--strip-trailing-cr",
            "-q",
            str(user_claude_md),
            str(default_claude_md),
        ],
        capture_output=True,
    )
    return "matches" if result.returncode == 0 else "customized"


def _handle_claude_md_propagation(tool_dir: Path, before: str, after: str, pre_state: str) -> None:
    """Handle default-CLAUDE.md propagation after a successful pull."""
    user_claude_md = tool_dir / ".claude_config" / ".claude" / "CLAUDE.md"
    default_claude_md = tool_dir / "ops" / "default-CLAUDE.md"

    _, rc = _git("diff", "--quiet", before, after, "--", "default-CLAUDE.md", cwd=str(tool_dir))
    if rc == 0:
        return  # No change to default-CLAUDE.md

    if pre_state == "matches":
        user_claude_md.unlink(missing_ok=True)
        print(
            f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} default-CLAUDE.md changed and your "
            ".claude_config/.claude/CLAUDE.md was unmodified; removed it so the "
            "next 'agent' run will install the new default."
        )
    elif pre_state == "customized":
        print()
        print(
            f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} default-CLAUDE.md changed upstream, but your "
            ".claude_config/.claude/CLAUDE.md has local customizations and was NOT touched."
        )
        print("To update it manually:")
        print(
            f"  1. Review the upstream change:  "
            f'git -C "{tool_dir}" diff {before} {after} -- default-CLAUDE.md'
        )
        print(f'  2. Compare with your copy:      diff -u "{user_claude_md}" "{default_claude_md}"')
        print(f'  3. Either merge the changes into "{user_claude_md}" by hand,')
        print("     or delete it to accept the new default (losing your customizations):")
        print(f'        rm "{user_claude_md}"')
        print("     The next 'agent' run will then copy the new default into place.")


_REBUILD_FILES = {
    "agent_wrap/commands/update.py",
    "agent_wrap/__main__.py",
    "ops/Dockerfile",
}

_RESOURCE_FILES = {
    "agent-wrap.bashrc",
}


def _changed_files(tool_dir: Path, before: str, after: str) -> set[str]:
    """Return the set of file paths changed between two commits."""
    out, rc = _git("diff", "--name-only", before, after, cwd=str(tool_dir))
    if rc != 0 or not out:
        return set()
    return set(out.splitlines())


def _resolve_ref(tool_dir: Path, commit: str) -> str:
    """Return a tag name if *commit* is tagged, otherwise its short hash."""
    tag, rc = _git("describe", "--tags", "--exact-match", commit, cwd=str(tool_dir))
    if rc == 0 and tag:
        return tag
    short, _rc = _git("rev-parse", "--short", commit, cwd=str(tool_dir))
    return short or commit


def apply(tool_dir: Path) -> int:
    """
    Pull updates and handle default-CLAUDE.md propagation.

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

    pre_state = _detect_claude_md_state(tool_dir)

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
        print(f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} already up to date; no action needed.")
        return 0

    ref = _resolve_ref(tool_dir, after)
    print(f"{Ansi.BOLD_YELLOW}Updated to {ref}{Ansi.RESET}")

    changed = _changed_files(tool_dir, before, after)

    if changed & _RESOURCE_FILES:
        print(
            f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} re-source agent-wrap.bashrc to pick up script changes."
        )

    if changed & _REBUILD_FILES:
        print(
            f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} run 'agent rebuild' to rebuild the Docker image"
            " with the updated files."
        )

    _handle_claude_md_propagation(tool_dir, before, after, pre_state)
    return 0


def run(args: list[str], tool_dir: Path) -> int:  # noqa: ARG001
    """Execute the `update` subcommand."""
    return apply(tool_dir)
