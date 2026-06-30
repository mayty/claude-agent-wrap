# This file has been edited with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

from __future__ import annotations

import os
import subprocess
import sys
from enum import Enum
from typing import TYPE_CHECKING

from agent_wrap.lib.argparsing import make_parser, parse_or_code
from agent_wrap.lib.console import Ansi
from agent_wrap.lib.utils import is_truthy_env


class _MdState(Enum):
    MATCHES = "matches"
    CUSTOMIZED = "customized"
    MISSING = "missing"


class _MdPropagation(Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    CONFLICT = "conflict"


if TYPE_CHECKING:
    from pathlib import Path

USAGE = ""
SUMMARY = "Pull upstream updates"


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


def _git_full(
    *args: str, cwd: str | None = None, timeout: int | None = None
) -> tuple[str, int, str]:
    """Run a git command and return (stdout, returncode, stderr)."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return result.stdout.strip(), result.returncode, result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", 1, ""


def _latest_tag(tool_dir: Path, ref: str) -> str | None:
    """Return the newest tag reachable from *ref*, or None if none/unavailable."""
    tag, rc = _git("describe", "--tags", "--abbrev=0", ref, cwd=str(tool_dir))
    if rc != 0 or not tag:
        return None
    return tag


def _resolve_target(tool_dir: Path, branch: str) -> str | None:
    """
    Resolve the ref `apply()` should fast-forward to, or None if no update fits.

    Non-master branches update to the branch tip on any upstream commit. master
    is gated on a newer tag: the target is the newest tag reachable from
    origin/master, and None when that tag is absent or unchanged.
    """
    if branch != "master":
        return f"origin/{branch}"

    local_tag = _latest_tag(tool_dir, "HEAD")
    upstream_tag = _latest_tag(tool_dir, f"origin/{branch}")
    if not upstream_tag or upstream_tag == local_tag:
        return None
    return upstream_tag


def _get_behind_count(tool_dir: Path) -> tuple[str, int, str] | None:
    """
    Return (branch, commits_behind, target_ref) if an update should be offered.

    The target_ref is what `apply()` should fast-forward to (see
    `_resolve_target`). Returns None whenever no suitable update exists — not a
    git repo, detached HEAD, fetch failure, no commits behind, or (on master) no
    newer tag.
    """
    _, rc = _git("rev-parse", "--git-dir", cwd=str(tool_dir))
    if rc != 0:
        return None

    branch, rc = _git("symbolic-ref", "--short", "HEAD", cwd=str(tool_dir))
    if rc != 0 or not branch:
        return None

    _, rc = _git("fetch", "--quiet", "--tags", "origin", branch, cwd=str(tool_dir), timeout=10)
    if rc != 0:
        return None

    behind, rc = _git("rev-list", "--count", f"HEAD..origin/{branch}", cwd=str(tool_dir))
    if rc != 0 or not behind or behind == "0":
        return None

    target_ref = _resolve_target(tool_dir, branch)
    if target_ref is None:
        return None

    return branch, int(behind), target_ref


def check(tool_dir: Path) -> bool:
    """
    Check for upstream updates. Returns True if an update was applied.

    Best-effort: any error (no network, detached HEAD, non-git dir, fetch
    failure) returns False so the caller's original command runs.
    """
    env_val = os.environ.get("AGENT_SKIP_UPDATE_CHECK", "")
    if is_truthy_env(env_val):
        return False

    result = _get_behind_count(tool_dir)
    if result is None:
        return False

    branch, behind, target_ref = result
    if branch == "master":
        print(
            f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} a new agent-wrap release "
            f"({target_ref}) is available."
        )
    else:
        print(
            f"{Ansi.BOLD_YELLOW}Note:{Ansi.RESET} agent-wrap is {behind} commit(s) "
            f"behind origin/{branch}."
        )
    try:
        ans = input("Update agent-wrap now? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False

    if ans.lower() != "y":
        return False

    apply(tool_dir, target_ref)
    return True


def _detect_claude_md_state(tool_dir: Path) -> _MdState:
    """Return the state of the user's CLAUDE.md relative to the default."""
    user_claude_md = tool_dir / ".claude_config" / ".claude" / "CLAUDE.md"
    default_claude_md = tool_dir / "ops" / "default-CLAUDE.md"
    if not (user_claude_md.exists() and default_claude_md.exists()):
        return _MdState.MISSING
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
    return _MdState.MATCHES if result.returncode == 0 else _MdState.CUSTOMIZED


def _handle_claude_md_propagation(
    tool_dir: Path, before: str, after: str, pre_state: _MdState
) -> _MdPropagation:
    """Handle default-CLAUDE.md propagation after a successful pull."""
    user_claude_md = tool_dir / ".claude_config" / ".claude" / "CLAUDE.md"

    _, rc = _git("diff", "--quiet", before, after, "--", "default-CLAUDE.md", cwd=str(tool_dir))
    if rc == 0:
        return _MdPropagation.UNCHANGED

    if pre_state == _MdState.MATCHES:
        user_claude_md.unlink(missing_ok=True)
        return _MdPropagation.UPDATED
    if pre_state == _MdState.CUSTOMIZED:
        return _MdPropagation.CONFLICT

    return _MdPropagation.UNCHANGED


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


def _print_status(tool_dir: Path, before: str, after: str, pre_state: _MdState) -> None:
    """Print post-update status summary."""
    before_ref = _resolve_ref(tool_dir, before)
    after_ref = _resolve_ref(tool_dir, after)
    print(f"{Ansi.BOLD_GREEN}Updated {before_ref} -> {after_ref}{Ansi.RESET}")

    changed = _changed_files(tool_dir, before, after)

    if changed & _RESOURCE_FILES:
        print(f"{Ansi.BOLD_YELLOW}re-source agent-wrap.bashrc to apply latest changes{Ansi.RESET}")
    else:
        print(f"{Ansi.BOLD_GREEN}no re-source needed{Ansi.RESET}")

    if changed & _REBUILD_FILES:
        print(f"{Ansi.BOLD_YELLOW}run 'agent rebuild' to apply latest changes{Ansi.RESET}")
    else:
        print(f"{Ansi.BOLD_GREEN}no re-build needed{Ansi.RESET}")

    md_status = _handle_claude_md_propagation(tool_dir, before, after, pre_state)
    if md_status == _MdPropagation.UPDATED:
        print(f"{Ansi.BOLD_GREEN}CLAUDE.md updated to new default{Ansi.RESET}")
    elif md_status == _MdPropagation.CONFLICT:
        print(
            f"{Ansi.BOLD_RED}CLAUDE.md changed upstream but you have local customizations{Ansi.RESET}"
        )
        print(
            f'{Ansi.BOLD_RED}Review: git -C "{tool_dir}" diff {before} {after}'
            f" -- default-CLAUDE.md{Ansi.RESET}"
        )
        print(
            f"{Ansi.BOLD_RED}Merge into .claude_config/.claude/CLAUDE.md"
            f" or delete it to accept the new default{Ansi.RESET}"
        )


def _fast_forward(tool_dir: Path, target_ref: str, before: str, pre_state: _MdState) -> int:
    """Fast-forward to *target_ref* and report. Returns 0 on success, 1 on failure."""
    # The fetch already ran (via _get_behind_count or the auto-check), so a
    # local fast-forward merge to the resolved target is sufficient.
    _, rc, stderr = _git_full("merge", "--ff-only", target_ref, cwd=str(tool_dir))
    if rc != 0:
        print(f"{Ansi.BOLD_RED}Update failed:{Ansi.RESET}")
        if stderr:
            print(stderr)
        return 1

    after, rc = _git("rev-parse", "HEAD", cwd=str(tool_dir))
    if rc != 0:
        return 1

    if before == after:
        print(f"{Ansi.BOLD_GREEN}Already up to date{Ansi.RESET}")
        return 0

    _print_status(tool_dir, before, after, pre_state)
    return 0


def apply(tool_dir: Path, target_ref: str | None = None) -> int:
    """
    Fast-forward to *target_ref* and handle default-CLAUDE.md propagation.

    When *target_ref* is None (direct `agent update`), the update target is
    recomputed via `_get_behind_count` so direct invocation honours the same
    tag-gating as the automatic check. Returns 0 on success, 1 on failure.
    """
    _, rc = _git("symbolic-ref", "--short", "HEAD", cwd=str(tool_dir))
    if rc != 0:
        print(f"{Ansi.BOLD_RED}Update failed:{Ansi.RESET}", file=sys.stderr)
        print("could not determine current branch", file=sys.stderr)
        return 1

    if target_ref is None:
        result = _get_behind_count(tool_dir)
        if result is None:
            print(f"{Ansi.BOLD_GREEN}Already up to date{Ansi.RESET}")
            return 0
        target_ref = result[2]

    before, rc = _git("rev-parse", "HEAD", cwd=str(tool_dir))
    if rc != 0:
        print(f"{Ansi.BOLD_RED}Update failed:{Ansi.RESET}", file=sys.stderr)
        print("could not get current HEAD", file=sys.stderr)
        return 1

    pre_state = _detect_claude_md_state(tool_dir)
    return _fast_forward(tool_dir, target_ref, before, pre_state)


def run(args: list[str], tool_dir: Path) -> int:
    """Execute the `update` subcommand."""
    ns = parse_or_code(make_parser("update", usage_summary=USAGE), args)
    if isinstance(ns, int):
        return ns
    return apply(tool_dir)
