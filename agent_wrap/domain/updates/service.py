# This file has been edited with the assistance of an AI tool.
"""Shared self-update logic — domain service."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_BOOTSTRAP_PATH,
    GLOBAL_CONFIG_DIR,
    OPS_DIR,
    TOOL_DIR,
    UpdateCheck,
)
from agent_wrap.domain.updates.constants import (
    BOOTSTRAP_FILES,
    REBUILD_FILES,
    RESOURCE_FILES,
    MdPropagation,
    MdState,
)
from agent_wrap.domain.updates.models import (
    BehindCountResult,
    GitFullResult,
    WrapperRevision,
)
from agent_wrap.lib.utils import is_truthy_env

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.logs.service import LogsService
    from agent_wrap.domain.sidecars.service import SidecarService


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
        except subprocess.TimeoutExpired, FileNotFoundError:
            return "", 1

    @staticmethod
    def git_full(*args: str, cwd: str | None = None) -> GitFullResult:
        """Run a git command and return (stdout, returncode, stderr)."""
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=cwd,
            )
            return GitFullResult(result.stdout.strip(), result.returncode, result.stderr.strip())
        except subprocess.TimeoutExpired, FileNotFoundError:
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

        if changed & BOOTSTRAP_FILES:
            display.warning("the pinned CPython moved — re-provisioning")
        else:
            display.success("no re-provision needed")

        md_status = _GitOps.handle_claude_md_propagation(before, after, pre_state)
        if md_status == MdPropagation.UPDATED:
            display.success("CLAUDE.md updated to new default")
        elif md_status == MdPropagation.CONFLICT:
            display.error(
                "CLAUDE.md changed upstream but you have local customizations\n"
                f'Review: git -C "{TOOL_DIR}" diff {before} {after} -- default-CLAUDE.md\n'
                "Merge into .claude_config/.claude/CLAUDE.md or delete it to accept the "
                "new default"
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
            display.error(f"update failed\n{stderr}" if stderr else "update failed")
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

    def __init__(
        self,
        display_service: DisplayService,
        logs_service: LogsService,
        sidecar_service: SidecarService,
    ) -> None:
        self._display = display_service
        self._logs = logs_service
        self._sidecars = sidecar_service

    def current_revision(self) -> WrapperRevision:
        """
        Resolve the installed wrapper's git identity, entirely from local state.

        Deliberately **no fetch**, unlike :meth:`check_updates`: this answers "what am I
        running", not "is there something newer", and a report must not make a network
        call. Every git invocation passes ``--no-optional-locks`` and a short timeout —
        ``status --porcelain`` refreshes the index by default, which would let a read-only
        command create ``.git/index.lock``, and an unresponsive git must not hang the
        report.

        Outside a git repo (a tarball install) every field is blank rather than an error.
        """
        branch, rc = _GitOps.git(
            "--no-optional-locks", "symbolic-ref", "--short", "HEAD", cwd=str(TOOL_DIR), timeout=5
        )
        if rc != 0 or not branch:
            # rev-parse tells "detached HEAD" apart from "not a git repo".
            _, head_rc = _GitOps.git(
                "--no-optional-locks", "rev-parse", "HEAD", cwd=str(TOOL_DIR), timeout=5
            )
            if head_rc != 0:
                return WrapperRevision(branch="", commit="", describe="", dirty=False)
            branch = "detached"

        commit, rc = _GitOps.git(
            "--no-optional-locks", "rev-parse", "--short", "HEAD", cwd=str(TOOL_DIR), timeout=5
        )
        if rc != 0:
            commit = ""

        describe, rc = _GitOps.git(
            "--no-optional-locks", "describe", "--tags", "--always", cwd=str(TOOL_DIR), timeout=5
        )
        if rc != 0:
            describe = ""

        porcelain, rc = _GitOps.git(
            "--no-optional-locks", "status", "--porcelain", cwd=str(TOOL_DIR), timeout=10
        )
        return WrapperRevision(
            branch=branch,
            commit=commit,
            describe=describe,
            dirty=rc == 0 and bool(porcelain.strip()),
        )

    def check_updates(self) -> UpdateCheck:
        """
        Check for upstream updates and, with the user's consent, apply one.

        Best-effort: any error (no network, detached HEAD, non-git dir, fetch failure)
        yields ``PROCEED`` so the caller's original command runs. ``BLOCKED`` is the one
        outcome that is not best-effort — see :meth:`_blocked_by_live_containers`.

        The env opt-out is read before anything else, so setting it means the caller
        neither prompts nor consults Docker. That is deliberate: it is the switch for a
        host that runs agents continuously and takes its updates on its own schedule.
        """
        env_val = os.environ.get("AGENT_SKIP_UPDATE_CHECK", "")
        if is_truthy_env(env_val):
            return UpdateCheck.PROCEED

        result = _GitOps.get_behind_count()
        if result is None:
            return UpdateCheck.PROCEED

        branch, behind, target_ref = result
        if branch == "master":
            self._display.warning(f"a new agent-wrap release ({target_ref}) is available.")
        else:
            self._display.warning(f"agent-wrap is {behind} commit(s) behind origin/{branch}.")

        # Before the prompt, not after: there is no point asking someone to confirm an
        # update that cannot run, and the refusal names what they have to stop.
        if self._blocked_by_live_containers():
            return UpdateCheck.BLOCKED

        if not self._display.prompt_confirm("Update agent-wrap now? [y/N]"):
            return UpdateCheck.PROCEED

        self.apply(target_ref)
        return UpdateCheck.HANDLED

    def _blocked_by_live_containers(self) -> bool:
        """
        Report whether an update must not start, naming whatever is holding it back.

        An update rewrites the checkout that every live agent's host-side code runs
        from and re-provisions the interpreter underneath it, so it is not something to
        do while a fleet is attached. Stopping the containers is the only way past this;
        there is no override flag on purpose.
        """
        live = self._sidecars.live_containers(TOOL_DIR)
        names = [c.name for c in live.agents] + [c.name for c in live.sidecars]
        if not names:
            return False
        listing = "\n  ".join(names)
        self._display.error(
            f"update refused: {len(names)} container(s) still running\n  {listing}\n"
            "Stop them and re-run."
        )
        return True

    def apply(self, target_ref: str | None = None) -> int:
        """
        Fast-forward to *target_ref* and handle default-CLAUDE.md propagation.

        When *target_ref* is None (direct ``agent update``), the update target is
        recomputed via ``_get_behind_count`` so direct invocation honours the same
        tag-gating as the automatic check. Returns 0 on success, 1 on failure.

        Refuses outright while any agent container or sidecar is running, but only once
        a merge is actually on the cards: an update with nothing to pull returns before
        the check, so the common no-op costs no Docker calls and leaves the viewer up.
        """
        _, rc = _GitOps.git("symbolic-ref", "--short", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0:
            self._display.error("update failed\ncould not determine current branch")
            return 1

        if target_ref is None:
            result = _GitOps.get_behind_count()
            if result is None:
                self._display.success("Already up to date")
                return 0
            target_ref = result.target_ref

        before, rc = _GitOps.git("rev-parse", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0:
            self._display.error("update failed\ncould not get current HEAD")
            return 1

        if self._blocked_by_live_containers():
            return 1

        pre_state = _GitOps.detect_claude_md_state()

        # The viewer is a long-lived process running wrapper code out of this checkout,
        # so it is stopped before the merge swaps that code underneath it rather than
        # after. Otherwise it spends the merge window executing newly merged code on an
        # interpreter that has not been re-provisioned yet — on the first migration that
        # pairing is a pre-3.11 interpreter running code that expects `datetime.UTC` —
        # and its stdio is /dev/null, so it would fail invisibly. The next `agent run`
        # restarts it.
        self._logs.stop_daemon()

        rc = _GitOps.fast_forward(target_ref, before, pre_state, self._display)
        if rc == 0:
            self._reprovision_interpreter(before)
        return rc

    def _reprovision_interpreter(self, before: str) -> None:
        """
        Re-run the bootstrap after an update that actually advanced HEAD.

        Invoked unconditionally rather than gated on ``BOOTSTRAP_FILES`` appearing in
        the diff: the bootstrap's own fast path makes it a few-millisecond no-op when
        the pin has not moved, which costs nothing and additionally repairs a previous
        bootstrap that was interrupted. Gating here would mean parsing the pin file in
        Python as well as in sh, for no gain.

        Best-effort by design. A failure leaves the *existing* interpreter in place and
        working, so it must not turn a successful update into a failed one — the message
        names the command to re-run.
        """
        after, rc = _GitOps.git("rev-parse", "HEAD", cwd=str(TOOL_DIR))
        if rc != 0 or after == before:
            return

        try:
            proc = subprocess.run(
                [str(AGENT_BOOTSTRAP_PATH)],
                cwd=str(TOOL_DIR),
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._display.error(
                f"could not provision the pinned CPython: {exc}\nRun: {AGENT_BOOTSTRAP_PATH}"
            )
            return
        if proc.returncode != 0:
            self._display.error(
                f"provisioning the pinned CPython failed\nRun: {AGENT_BOOTSTRAP_PATH}"
            )
