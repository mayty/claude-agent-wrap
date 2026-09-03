# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/commands/update.py."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import Mock

    import pytest
    import pytest_mock

from typing import Any

import pytest

from agent_wrap.constants import AGENT_BOOTSTRAP_PATH, RUNNING_STATUS, UpdateCheck
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.sidecars.models import (
    AgentContainer,
    LiveContainers,
    SidecarContainer,
)
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.updates.constants import MdPropagation, MdState
from agent_wrap.domain.updates.service import UpdateService, _GitOps


@pytest.fixture
def logs_mock(mocker: pytest_mock.MockFixture) -> Mock:
    """Return an autospecced LogsService, which UpdateService stops before merging."""
    return mocker.create_autospec(LogsService, instance=True)


@pytest.fixture
def sidecar_mock(mocker: pytest_mock.MockFixture) -> Mock:
    """Return an autospecced SidecarService reporting an idle host, the common case."""
    sidecars = mocker.create_autospec(SidecarService, instance=True)
    sidecars.live_containers.return_value = LiveContainers(agents=[], sidecars=[])
    return sidecars


@pytest.fixture
def update_svc(display_mock: Mock, logs_mock: Mock, sidecar_mock: Mock) -> UpdateService:
    """Return an UpdateService with the shared display_mock."""
    return UpdateService(
        display_service=display_mock,
        logs_service=logs_mock,
        sidecar_service=sidecar_mock,
    )


def test_get_behind_not_git_dir(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, return_value=("", 1)
    )
    assert _GitOps.get_behind_count() is None


def test_get_behind_detached_head(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("", 1)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    assert _GitOps.get_behind_count() is None


def test_get_behind_three_commits_non_master(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("3", 0)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    result = _GitOps.get_behind_count()
    assert result == ("main", 3, "origin/main")


def test_get_behind_fetches_tags(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("1", 0)
        return ("", 0)

    mock_git = mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    _GitOps.get_behind_count()
    fetch_calls = [c for c in mock_git.call_args_list if c.args and c.args[0] == "fetch"]
    assert fetch_calls
    assert "--tags" in fetch_calls[0].args


def test_get_behind_master_new_tag(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("master", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("3", 0)
        if args[0] == "describe":
            ref = args[-1]
            return ("v1.1" if ref.startswith("origin/") else "v1.0", 0)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    result = _GitOps.get_behind_count()
    assert result == ("master", 3, "v1.1")


def test_get_behind_master_no_new_tag(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("master", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("3", 0)
        if args[0] == "describe":
            return ("v1.0", 0)  # same tag local and upstream
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    assert _GitOps.get_behind_count() is None


def test_get_behind_master_no_upstream_tag(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("master", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("3", 0)
        if args[0] == "describe":
            return ("", 1)  # no tags reachable
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    assert _GitOps.get_behind_count() is None


def test_get_behind_fetch_fails(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 1)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    assert _GitOps.get_behind_count() is None


def test_get_behind_no_commits(
    mocker: pytest_mock.MockFixture,
) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("0", 0)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    assert _GitOps.get_behind_count() is None


def test_check_skip_env_set(monkeypatch: pytest.MonkeyPatch, update_svc: UpdateService) -> None:
    monkeypatch.setenv("AGENT_SKIP_UPDATE_CHECK", "1")
    assert update_svc.check_updates() is UpdateCheck.PROCEED


def test_check_no_behind(mocker: pytest_mock.MockFixture, update_svc: UpdateService) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=None,
    )
    assert update_svc.check_updates() is UpdateCheck.PROCEED


def test_check_user_says_no(
    mocker: pytest_mock.MockFixture, display_mock: Mock, update_svc: UpdateService
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=("main", 2, "origin/main"),
    )
    display_mock.prompt_confirm.return_value = False
    mock_apply = mocker.patch.object(UpdateService, "apply", autospec=True)
    assert update_svc.check_updates() is UpdateCheck.PROCEED
    mock_apply.assert_not_called()


def test_check_user_says_yes(
    mocker: pytest_mock.MockFixture, display_mock: Mock, update_svc: UpdateService
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=("main", 2, "origin/main"),
    )
    display_mock.prompt_confirm.return_value = True
    mock_apply = mocker.patch.object(UpdateService, "apply", autospec=True)
    assert update_svc.check_updates() is UpdateCheck.HANDLED
    mock_apply.assert_called_once_with(update_svc, "origin/main")


def test_check_master_announces_tag(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=("master", 3, "v1.1"),
    )
    display_mock.prompt_confirm.return_value = True
    mock_apply = mocker.patch.object(UpdateService, "apply", autospec=True)
    assert update_svc.check_updates() is UpdateCheck.HANDLED
    mock_apply.assert_called_once_with(update_svc, "v1.1")
    display_mock.warning.assert_any_call("a new agent-wrap release (v1.1) is available.")


def test_check_eof_error(
    mocker: pytest_mock.MockFixture, display_mock: Mock, update_svc: UpdateService
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=("main", 2, "origin/main"),
    )
    display_mock.prompt_confirm.return_value = False
    assert update_svc.check_updates() is UpdateCheck.PROCEED


def test_detect_matches(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    default_md = ops_dir / "default-CLAUDE.md"
    content = "# hello"
    user_md.write_text(content)
    default_md.write_text(content)
    assert _GitOps.detect_claude_md_state() == MdState.MATCHES


def test_detect_customized(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    default_md = ops_dir / "default-CLAUDE.md"
    user_md.write_text("# user version")
    default_md.write_text("# default version")
    assert _GitOps.detect_claude_md_state() == MdState.CUSTOMIZED


def test_detect_missing() -> None:
    assert _GitOps.detect_claude_md_state() == MdState.MISSING


def test_propagation_no_diff(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, return_value=("", 0)
    )
    result = _GitOps.handle_claude_md_propagation("abc", "def", MdState.MATCHES)
    assert result == MdPropagation.UNCHANGED


def test_propagation_matches_deletes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# user content")
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, return_value=("", 1)
    )
    result = _GitOps.handle_claude_md_propagation("abc", "def", MdState.MATCHES)
    assert result == MdPropagation.UPDATED
    assert not user_md.exists()


def test_propagation_customized_returns_conflict(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# custom")
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, return_value=("", 1)
    )
    result = _GitOps.handle_claude_md_propagation("abc", "def", MdState.CUSTOMIZED)
    assert result == MdPropagation.CONFLICT
    assert user_md.exists()


def test_apply_cannot_determine_branch(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, return_value=("", 1)
    )
    rc = update_svc.apply()
    assert rc == 1
    display_mock.error.assert_called_once_with("update failed\ncould not determine current branch")


def test_apply_cannot_get_head(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mock_git = mocker.patch("agent_wrap.domain.updates.service._GitOps.git", autospec=True)
    mock_git.side_effect = [
        ("main", 0),  # symbolic-ref ok
        ("", 1),  # rev-parse HEAD fails
    ]
    rc = update_svc.apply("origin/main")
    assert rc == 1
    display_mock.error.assert_called_once_with("update failed\ncould not get current HEAD")


def test_apply_merge_fails(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    mock_full = mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 1, "fatal: not possible to fast-forward"),
    )
    rc = update_svc.apply("origin/main")
    assert rc == 1
    display_mock.error.assert_called_once_with("update failed\nfatal: not possible to fast-forward")
    # Fast-forwards to the resolved target ref, not a raw branch pull.
    assert mock_full.call_args.args == ("merge", "--ff-only", "origin/main")


def test_apply_merges_to_tag_target(
    mocker: pytest_mock.MockFixture, update_svc: UpdateService
) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "symbolic-ref":
            return ("master", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    mock_full = mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )
    mocker.patch("subprocess.run").return_value.returncode = 0
    rc = update_svc.apply("v1.1")
    assert rc == 0
    assert mock_full.call_args.args == ("merge", "--ff-only", "v1.1")


def test_apply_recomputes_target_when_none(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, return_value=("master", 0)
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=None,
    )
    rc = update_svc.apply()
    assert rc == 0
    display_mock.success.assert_any_call("Already up to date")


def test_apply_already_up_to_date(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        return ("", 0)

    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git", autospec=True, side_effect=fake_git
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )
    mocker.patch("subprocess.run").return_value.returncode = 0
    rc = update_svc.apply("origin/main")
    assert rc == 0
    display_mock.success.assert_any_call("Already up to date")


# --- current_revision (local-only, read-only) ---

_GIT = "agent_wrap.domain.updates.service._GitOps.git"


def _fake_git(responses: dict[str, tuple[str, int]]):
    """Build a _GitOps.git stub keyed on the git subcommand (after global flags)."""

    def fake(*args: Any, **_: Any) -> tuple[str, int]:
        subcommand = next((a for a in args if not a.startswith("-")), "")
        return responses.get(subcommand, ("", 0))

    return fake


def test_current_revision_reports_branch_commit_and_describe(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(
        _GIT,
        autospec=True,
        side_effect=_fake_git(
            {
                "symbolic-ref": ("master", 0),
                "rev-parse": ("7e8ef2f", 0),
                "describe": ("0.8.0", 0),
                "status": ("", 0),
            }
        ),
    )
    revision = update_svc.current_revision()
    assert revision.branch == "master"
    assert revision.commit == "7e8ef2f"
    assert revision.describe == "0.8.0"
    assert revision.dirty is False


def test_current_revision_flags_dirty_worktree(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(
        _GIT,
        autospec=True,
        side_effect=_fake_git(
            {
                "symbolic-ref": ("master", 0),
                "rev-parse": ("7e8ef2f", 0),
                "describe": ("0.8.0", 0),
                "status": (" M agent_wrap/x.py", 0),
            }
        ),
    )
    assert update_svc.current_revision().dirty is True


def test_current_revision_detached_head(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(
        _GIT,
        autospec=True,
        side_effect=_fake_git(
            {"symbolic-ref": ("", 1), "rev-parse": ("7e8ef2f", 0), "describe": ("0.8.0", 0)}
        ),
    )
    assert update_svc.current_revision().branch == "detached"


def test_current_revision_blank_outside_a_git_repo(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_GIT, autospec=True, return_value=("", 1))
    revision = update_svc.current_revision()
    assert revision.branch == ""
    assert revision.commit == ""
    assert revision.describe == ""
    assert revision.dirty is False


def test_current_revision_never_fetches(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    """A report must not reach the network to say what revision is installed."""
    git = mocker.patch(_GIT, autospec=True, return_value=("ok", 0))
    update_svc.current_revision()
    subcommands = [call.args for call in git.call_args_list]
    assert not any("fetch" in args for args in subcommands)


def test_current_revision_passes_no_optional_locks(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    """Otherwise `status --porcelain` refreshes the index and creates .git/index.lock."""
    git = mocker.patch(_GIT, autospec=True, return_value=("ok", 0))
    update_svc.current_revision()
    assert git.call_args_list
    for call in git.call_args_list:
        assert "--no-optional-locks" in call.args


def test_current_revision_bounds_every_git_call(
    update_svc: UpdateService, mocker: pytest_mock.MockFixture
) -> None:
    """A wedged git must not hang the report."""
    git = mocker.patch(_GIT, autospec=True, return_value=("ok", 0))
    update_svc.current_revision()
    for call in git.call_args_list:
        assert call.kwargs["timeout"] > 0


def _agent_container(name: str) -> AgentContainer:
    """Return a running agent container; only *name* and *status* reach the update gate."""
    return AgentContainer(
        name=name,
        instance_id=name.removeprefix("claude-agent-"),
        status=RUNNING_STATUS,
        uptime_sec=60,
        cwd="/home/me/thing",
        image="claude-agent",
        provider="litellm-anthropic",
        sidecars=[],
    )


def _sidecar_container(name: str) -> SidecarContainer:
    """Return a running sidecar container, trimmed to what the update gate reads."""
    return SidecarContainer(
        name=name,
        role="litellm",
        provider="litellm-anthropic",
        status=RUNNING_STATUS,
        health="healthy",
        uptime_sec=60,
        port=48620,
        exit_code=None,
        image="litellm:latest",
        stale_image=False,
        networks=["agent-wrap-net"],
    )


def _advancing_git(before: str, after: str):
    """Return a fake _GitOps.git where rev-parse reports `before` then `after`."""
    seen: list[str] = []

    def fake_git(*args: Any, **_: Any) -> tuple[str, int]:
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            seen.append(args[0])
            return (before, 0) if len(seen) == 1 else (after, 0)
        return ("", 0)

    return fake_git


@pytest.fixture
def bootstrap_run(mocker: pytest_mock.MockFixture) -> Mock:
    """Patch the subprocess the re-provision step shells out to."""
    return mocker.patch(
        "agent_wrap.domain.updates.service.subprocess.run",
        autospec=True,
        return_value=mocker.Mock(returncode=0),
    )


def test_apply_reprovisions_the_interpreter_after_advancing(
    mocker: pytest_mock.MockFixture,
    update_svc: UpdateService,
    bootstrap_run: Mock,
) -> None:
    """A stale pin would leave users on an unpatched CPython, so this is not advisory."""
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "bbb222"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.print_status", autospec=True, return_value=None
    )

    assert update_svc.apply("origin/main") == 0
    assert bootstrap_run.call_count == 1
    assert bootstrap_run.call_args.args[0] == [str(AGENT_BOOTSTRAP_PATH)]
    assert bootstrap_run.call_args.kwargs["timeout"] > 0


def test_apply_stops_the_logs_daemon_before_merging(
    mocker: pytest_mock.MockFixture,
    update_svc: UpdateService,
    logs_mock: Mock,
    bootstrap_run: Mock,
) -> None:
    """Ordering is the point: after the merge the viewer already runs the new code."""
    order: list[str] = []

    def fake_merge(*_args: Any, **_kwargs: Any) -> tuple[str, int, str]:
        order.append("merge")
        return ("", 0, "")

    logs_mock.stop_daemon.side_effect = lambda: order.append("stop")
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "bbb222"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        side_effect=fake_merge,
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.print_status", autospec=True, return_value=None
    )

    update_svc.apply("origin/main")
    assert order == ["stop", "merge"]
    assert bootstrap_run.called


def test_apply_stops_the_logs_daemon_even_when_the_merge_fails(
    mocker: pytest_mock.MockFixture,
    update_svc: UpdateService,
    logs_mock: Mock,
    bootstrap_run: Mock,
) -> None:
    """Stopping first means a failed merge costs a viewer restart -- the accepted price."""
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "aaa111"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 1, "not a fast-forward"),
    )

    assert update_svc.apply("origin/main") == 1
    logs_mock.stop_daemon.assert_called_once()
    assert not bootstrap_run.called


def test_apply_does_not_reprovision_when_the_merge_advances_nothing(
    mocker: pytest_mock.MockFixture,
    update_svc: UpdateService,
    bootstrap_run: Mock,
) -> None:
    """A merge that moved no commits leaves the pinned interpreter alone."""
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "aaa111"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )

    assert update_svc.apply("origin/main") == 0
    assert not bootstrap_run.called


def test_apply_leaves_the_viewer_alone_when_there_is_nothing_to_merge(
    mocker: pytest_mock.MockFixture,
    update_svc: UpdateService,
    logs_mock: Mock,
    sidecar_mock: Mock,
    bootstrap_run: Mock,
) -> None:
    """The no-op `agent update` is the common one: no Docker call, no viewer restart."""
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "aaa111"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=None,
    )

    assert update_svc.apply() == 0
    logs_mock.stop_daemon.assert_not_called()
    sidecar_mock.live_containers.assert_not_called()
    assert not bootstrap_run.called


def test_apply_refuses_while_containers_are_live(  # noqa: PLR0913
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
    logs_mock: Mock,
    sidecar_mock: Mock,
    bootstrap_run: Mock,
) -> None:
    """Swapping the checkout under an attached fleet is the thing being prevented."""
    sidecar_mock.live_containers.return_value = LiveContainers(
        agents=[_agent_container("claude-agent-7f3")],
        sidecars=[_sidecar_container("agent-wrap-litellm-anthropic")],
    )
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "bbb222"))
    merge = mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )

    assert update_svc.apply("origin/main") == 1
    merge.assert_not_called()
    logs_mock.stop_daemon.assert_not_called()
    assert not bootstrap_run.called
    message = display_mock.error.call_args.args[0]
    assert "claude-agent-7f3" in message
    assert "agent-wrap-litellm-anthropic" in message


def test_check_updates_refuses_before_prompting_while_containers_are_live(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
    sidecar_mock: Mock,
) -> None:
    """Nobody should be asked to confirm an update that is already refused."""
    sidecar_mock.live_containers.return_value = LiveContainers(
        agents=[_agent_container("claude-agent-7f3")], sidecars=[]
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        autospec=True,
        return_value=("main", 2, "origin/main"),
    )
    mock_apply = mocker.patch.object(UpdateService, "apply", autospec=True)

    assert update_svc.check_updates() is UpdateCheck.BLOCKED
    display_mock.prompt_confirm.assert_not_called()
    mock_apply.assert_not_called()


def test_apply_survives_a_failing_bootstrap(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
    bootstrap_run: Mock,
) -> None:
    """The already-installed interpreter still works, so the update itself succeeded."""
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "bbb222"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.print_status", autospec=True, return_value=None
    )
    bootstrap_run.return_value = mocker.Mock(returncode=1)

    assert update_svc.apply("origin/main") == 0
    assert str(AGENT_BOOTSTRAP_PATH) in display_mock.error.call_args.args[0]


def test_apply_reports_an_unrunnable_bootstrap(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
    bootstrap_run: Mock,
) -> None:
    """A missing or non-executable script must name the command, not raise."""
    mocker.patch(_GIT, autospec=True, side_effect=_advancing_git("aaa111", "bbb222"))
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        autospec=True,
        return_value=("", 0, ""),
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.print_status", autospec=True, return_value=None
    )
    bootstrap_run.side_effect = OSError("Permission denied")

    assert update_svc.apply("origin/main") == 0
    assert "Permission denied" in display_mock.error.call_args.args[0]


@pytest.mark.parametrize(
    "changed",
    [
        {"python-pin.env"},
        {"bin/agent-bootstrap"},
        {"bin/requirements.txt"},
    ],
)
def test_print_status_announces_a_reprovision_for_every_bootstrap_file(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    changed: set[str],
) -> None:
    """Constraints belong in that set too: stale ones leave new code importing nothing."""
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.resolve_ref", autospec=True, return_value="ref"
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.changed_files",
        autospec=True,
        return_value=changed,
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.handle_claude_md_propagation",
        autospec=True,
        return_value=MdPropagation.UNCHANGED,
    )

    _GitOps.print_status("aaa111", "bbb222", MdState.MATCHES, display_mock)

    warnings = [call.args[0] for call in display_mock.warning.call_args_list]
    assert any("re-provisioning" in text for text in warnings)


def test_print_status_stays_quiet_when_no_bootstrap_file_moved(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
) -> None:
    """The common update touches none of them, and must not suggest work that is not needed."""
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.resolve_ref", autospec=True, return_value="ref"
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.changed_files",
        autospec=True,
        return_value={"README.md"},
    )
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.handle_claude_md_propagation",
        autospec=True,
        return_value=MdPropagation.UNCHANGED,
    )

    _GitOps.print_status("aaa111", "bbb222", MdState.MATCHES, display_mock)

    successes = [call.args[0] for call in display_mock.success.call_args_list]
    assert "no re-provision needed" in successes
