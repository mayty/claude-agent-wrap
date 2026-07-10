# This file has been created with the assistance of an AI tool.
# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/commands/update.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import Mock

    import pytest
    import pytest_mock

from typing import Any

import pytest

from agent_wrap.domain.updates.models import MdPropagation, MdState
from agent_wrap.domain.updates.service import UpdateService, _GitOps


@pytest.fixture
def update_svc(display_mock: Mock) -> UpdateService:
    """Return an UpdateService with the shared display_mock."""
    return UpdateService(display_service=display_mock)


# --- _get_behind_count ---


def test_get_behind_not_git_dir(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", return_value=("", 1))
    assert _GitOps.get_behind_count() is None


def test_get_behind_detached_head(mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args: Any, **_: Any):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("", 1)
        return ("", 0)

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mock_git = mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
    assert _GitOps.get_behind_count() is None


# --- check_updates ---


def test_check_skip_env_set(monkeypatch: pytest.MonkeyPatch, update_svc: UpdateService) -> None:
    monkeypatch.setenv("AGENT_SKIP_UPDATE_CHECK", "1")
    assert update_svc.check_updates() is False


def test_check_no_behind(mocker: pytest_mock.MockFixture, update_svc: UpdateService) -> None:
    mocker.patch("agent_wrap.domain.updates.service._GitOps.get_behind_count", return_value=None)
    assert update_svc.check_updates() is False


def test_check_user_says_no(
    mocker: pytest_mock.MockFixture, display_mock: Mock, update_svc: UpdateService
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        return_value=("main", 2, "origin/main"),
    )
    display_mock.prompt_confirm.return_value = False
    mock_apply = mocker.patch.object(UpdateService, "apply")
    assert update_svc.check_updates() is False
    mock_apply.assert_not_called()


def test_check_user_says_yes(
    mocker: pytest_mock.MockFixture, display_mock: Mock, update_svc: UpdateService
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        return_value=("main", 2, "origin/main"),
    )
    display_mock.prompt_confirm.return_value = True
    mock_apply = mocker.patch.object(UpdateService, "apply")
    assert update_svc.check_updates() is True
    mock_apply.assert_called_once_with("origin/main")


def test_check_master_announces_tag(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        return_value=("master", 3, "v1.1"),
    )
    display_mock.prompt_confirm.return_value = True
    mock_apply = mocker.patch.object(UpdateService, "apply")
    assert update_svc.check_updates() is True
    mock_apply.assert_called_once_with("v1.1")
    display_mock.warning.assert_any_call("a new agent-wrap release (v1.1) is available.")


def test_check_eof_error(
    mocker: pytest_mock.MockFixture, display_mock: Mock, update_svc: UpdateService
) -> None:
    mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.get_behind_count",
        return_value=("main", 2, "origin/main"),
    )
    display_mock.prompt_confirm.return_value = False
    assert update_svc.check_updates() is False


# --- _detect_claude_md_state ---


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


# --- _handle_claude_md_propagation ---


def test_propagation_no_diff(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", return_value=("", 0))
    result = _GitOps.handle_claude_md_propagation("abc", "def", MdState.MATCHES)
    assert result == MdPropagation.UNCHANGED


def test_propagation_matches_deletes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# user content")
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", return_value=("", 1))
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
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", return_value=("", 1))
    result = _GitOps.handle_claude_md_propagation("abc", "def", MdState.CUSTOMIZED)
    assert result == MdPropagation.CONFLICT
    assert user_md.exists()


# --- apply ---


def test_apply_cannot_determine_branch(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", return_value=("", 1))
    rc = update_svc.apply()
    assert rc == 1
    display_mock.error.assert_any_call("Update failed:")
    display_mock.error.assert_any_call("could not determine current branch")


def test_apply_cannot_get_head(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    update_svc: UpdateService,
) -> None:
    mock_git = mocker.patch("agent_wrap.domain.updates.service._GitOps.git")
    mock_git.side_effect = [
        ("main", 0),  # symbolic-ref ok
        ("", 1),  # rev-parse HEAD fails
    ]
    rc = update_svc.apply("origin/main")
    assert rc == 1
    display_mock.error.assert_any_call("Update failed:")
    display_mock.error.assert_any_call("could not get current HEAD")


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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
    mock_full = mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full",
        return_value=("", 1, "fatal: not possible to fast-forward"),
    )
    rc = update_svc.apply("origin/main")
    assert rc == 1
    display_mock.error.assert_any_call("Update failed:")
    display_mock.error.assert_any_call("fatal: not possible to fast-forward")
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
    mock_full = mocker.patch(
        "agent_wrap.domain.updates.service._GitOps.git_full", return_value=("", 0, "")
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
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", return_value=("master", 0))
    mocker.patch("agent_wrap.domain.updates.service._GitOps.get_behind_count", return_value=None)
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

    mocker.patch("agent_wrap.domain.updates.service._GitOps.git", side_effect=fake_git)
    mocker.patch("agent_wrap.domain.updates.service._GitOps.git_full", return_value=("", 0, ""))
    mocker.patch("subprocess.run").return_value.returncode = 0
    rc = update_svc.apply("origin/main")
    assert rc == 0
    display_mock.success.assert_any_call("Already up to date")
