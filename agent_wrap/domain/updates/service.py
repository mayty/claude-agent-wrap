# This file has been edited with the assistance of an AI tool.
"""Shared self-update logic — domain service."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from agent_wrap.constants import GLOBAL_CONFIG_DIR, OPS_DIR, TOOL_DIR
from agent_wrap.domain.updates.constants import REBUILD_FILES, RESOURCE_FILES
from agent_wrap.domain.updates.models import (
    BehindCountResult,
    GitFullResult,
    MdPropagation,
    MdState,
)
from agent_wrap.lib.utils import is_truthy_env

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


class _GitOps:
    """Git operations for update detection and application."""

    @staticmethod
    def git(*args: str, cwd: str | None = None, timeout: int | None = None) -> tuple[str, int]:
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

    @staticmethod
    def git_full(*args: str, cwd: str | None = None, timeout: int | None = None) -> GitFullResult:
        """Run a git command and return (stdout, returncode, stderr)."""
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
            )
            return GitFullResult(result.stdout.strip(), result.returncode, result.stderr.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return GitFullResult("", 1, "")

    @staticmethod
    def latest_tag(ref: str) -> str | None:
        """Return the newest tag reachable from *ref*, or None if none/unavailable."""
        tag, rc = _GitOps.git("describe", "--tags", "--abbrev=0", ref, cwd=str(TOOL_DIR))
        if rc != 0 or not tag:
            return None
        return tag

    @staticmethod
    def resolve_target(branch: str) -> str | None:
        """
        Resolve the ref `apply()` should fast-forward to, or None if no update fits.

        Non-master branches update to the branch tip on any upstream commit. master
        is gated on a newer tag: the target is the newest tag reachable from
        origin/master, and None when that tag is absent or unchanged.
        """
        if branch != "master":
            return f"origin/{branch}"

        local_tag = _GitOps.latest_tag("HEAD")
        upstream_tag = _GitOps.latest_tag(f"origin/{branch}")
        if not upstream_tag or upstream_tag == local_tag:
            return None
        return upstream_tag

    @staticmethod
    def get_behind_count() -> BehindCountResult | None:
        """
        Return a ``BehindCountResult`` if an update should be offered.

        The target_ref is what `apply()` should fast-forward to (see
        `_resolve_target`). Returns None whenever no suitable update exists — not a
        git repo, detached HEAD, fetch failure, no commits behind, or (on master) no
        newer tag.
        """
        _, rc = _GitOps.git("rev-parse", "--git-dir", cwd=str(TOOL_DIR))
        if rc != 0:
            return None

        branch, rc = _GitOps.git("symbolic-ref", "--short", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0 or not branch:
            return None

        _, rc = _GitOps.git(
            "fetch",
            "--quiet",
            "--tags",
            "origin",
            branch,
            cwd=str(TOOL_DIR),
            timeout=10,
        )
        if rc != 0:
            return None

        behind, rc = _GitOps.git("rev-list", "--count", f"HEAD..origin/{branch}", cwd=str(TOOL_DIR))
        if rc != 0 or not behind or behind == "0":
            return None

        target_ref = _GitOps.resolve_target(branch)
        if target_ref is None:
            return None

        return BehindCountResult(branch, int(behind), target_ref)

    @staticmethod
    def detect_claude_md_state() -> MdState:
        """Return the state of the user's CLAUDE.md relative to the default."""
        user_claude_md = GLOBAL_CONFIG_DIR / ".claude" / "CLAUDE.md"
        default_claude_md = OPS_DIR / "default-CLAUDE.md"
        if not (user_claude_md.exists() and default_claude_md.exists()):
            return MdState.MISSING
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
        return MdState.MATCHES if result.returncode == 0 else MdState.CUSTOMIZED

    @staticmethod
    def handle_claude_md_propagation(before: str, after: str, pre_state: MdState) -> MdPropagation:
        """Handle default-CLAUDE.md propagation after a successful pull."""
        user_claude_md = GLOBAL_CONFIG_DIR / ".claude" / "CLAUDE.md"

        _, rc = _GitOps.git(
            "diff",
            "--quiet",
            before,
            after,
            "--",
            "default-CLAUDE.md",
            cwd=str(TOOL_DIR),
        )
        if rc == 0:
            return MdPropagation.UNCHANGED

        if pre_state == MdState.MATCHES:
            user_claude_md.unlink(missing_ok=True)
            return MdPropagation.UPDATED
        if pre_state == MdState.CUSTOMIZED:
            return MdPropagation.CONFLICT

        return MdPropagation.UNCHANGED

    @staticmethod
    def changed_files(before: str, after: str) -> set[str]:
        """Return the set of file paths changed between two commits."""
        out, rc = _GitOps.git("diff", "--name-only", before, after, cwd=str(TOOL_DIR))
        if rc != 0 or not out:
            return set()
        return set(out.splitlines())

    @staticmethod
    def resolve_ref(commit: str) -> str:
        """Return a tag name if *commit* is tagged, otherwise its short hash."""
        tag, rc = _GitOps.git("describe", "--tags", "--exact-match", commit, cwd=str(TOOL_DIR))
        if rc == 0 and tag:
            return tag
        short, _rc = _GitOps.git("rev-parse", "--short", commit, cwd=str(TOOL_DIR))
        return short or commit

    @staticmethod
    def print_status(before: str, after: str, pre_state: MdState, display: DisplayService) -> None:
        """Print post-update status summary."""
        before_ref = _GitOps.resolve_ref(before)
        after_ref = _GitOps.resolve_ref(after)
        display.success(f"Updated {before_ref} -> {after_ref}")

        changed = _GitOps.changed_files(before, after)

        if changed & RESOURCE_FILES:
            display.warning("re-source agent-wrap.bashrc to apply latest changes")
        else:
            display.success("no re-source needed")

        if changed & REBUILD_FILES:
            display.warning("run 'agent rebuild' to apply latest changes")
        else:
            display.success("no re-build needed")

        md_status = _GitOps.handle_claude_md_propagation(before, after, pre_state)
        if md_status == MdPropagation.UPDATED:
            display.success("CLAUDE.md updated to new default")
        elif md_status == MdPropagation.CONFLICT:
            display.error("CLAUDE.md changed upstream but you have local customizations")
            display.error(f'Review: git -C "{TOOL_DIR}" diff {before} {after} -- default-CLAUDE.md')
            display.error(
                "Merge into .claude_config/.claude/CLAUDE.md or delete it to accept the new default"
            )

    @staticmethod
    def fast_forward(
        target_ref: str, before: str, pre_state: MdState, display: DisplayService
    ) -> int:
        """
        Fast-forward to *target_ref* and report. Returns 0 on success, 1 on
        failure.
        """
        # The fetch already ran (via _get_behind_count or the auto-check), so a
        # local fast-forward merge to the resolved target is sufficient.
        _, rc, stderr = _GitOps.git_full("merge", "--ff-only", target_ref, cwd=str(TOOL_DIR))
        if rc != 0:
            display.error("Update failed:")
            if stderr:
                display.error(stderr)
            return 1

        after, rc = _GitOps.git("rev-parse", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0:
            return 1

        if before == after:
            display.success("Already up to date")
            return 0

        _GitOps.print_status(before, after, pre_state, display)
        return 0


class UpdateService:
    """Git-based self-update logic for agent-wrap."""

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    def check_updates(self) -> bool:
        """
        Check for upstream updates. Returns True if an update was applied.

        Best-effort: any error (no network, detached HEAD, non-git dir, fetch
        failure) returns False so the caller's original command runs.
        """
        env_val = os.environ.get("AGENT_SKIP_UPDATE_CHECK", "")
        if is_truthy_env(env_val):
            return False

        result = _GitOps.get_behind_count()
        if result is None:
            return False

        branch, behind, target_ref = result
        if branch == "master":
            self._display.warning(f"a new agent-wrap release ({target_ref}) is available.")
        else:
            self._display.warning(f"agent-wrap is {behind} commit(s) behind origin/{branch}.")

        if not self._display.prompt_confirm("Update agent-wrap now? [y/N]"):
            return False

        self.apply(target_ref)
        return True

    def apply(self, target_ref: str | None = None) -> int:
        """
        Fast-forward to *target_ref* and handle default-CLAUDE.md propagation.

        When *target_ref* is None (direct ``agent update``), the update target is
        recomputed via ``_get_behind_count`` so direct invocation honours the same
        tag-gating as the automatic check. Returns 0 on success, 1 on failure.
        """
        _, rc = _GitOps.git("symbolic-ref", "--short", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0:
            self._display.error("Update failed:")
            self._display.error("could not determine current branch")
            return 1

        if target_ref is None:
            result = _GitOps.get_behind_count()
            if result is None:
                self._display.success("Already up to date")
                return 0
            target_ref = result.target_ref

        before, rc = _GitOps.git("rev-parse", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0:
            self._display.error("Update failed:")
            self._display.error("could not get current HEAD")
            return 1

        pre_state = _GitOps.detect_claude_md_state()
        return _GitOps.fast_forward(target_ref, before, pre_state, self._display)
