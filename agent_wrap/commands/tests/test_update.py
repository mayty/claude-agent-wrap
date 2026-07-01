# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/commands/update.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.commands.update import (
    _detect_claude_md_state,
    _get_behind_count,
    _handle_claude_md_propagation,
    _MdPropagation,
    _MdState,
    apply,
    check,
)

# --- _get_behind_count ---


def test_get_behind_not_git_dir(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    assert _get_behind_count() is None


def test_get_behind_detached_head(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("", 1)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    assert _get_behind_count() is None


def test_get_behind_three_commits_non_master(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("3", 0)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    result = _get_behind_count()
    assert result == ("main", 3, "origin/main")


def test_get_behind_fetches_tags(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("1", 0)
        return ("", 0)

    mock_git = mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    _get_behind_count()
    fetch_calls = [c for c in mock_git.call_args_list if c.args and c.args[0] == "fetch"]
    assert fetch_calls
    assert "--tags" in fetch_calls[0].args


def test_get_behind_master_new_tag(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
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

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    result = _get_behind_count()
    assert result == ("master", 3, "v1.1")


def test_get_behind_master_no_new_tag(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
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

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    assert _get_behind_count() is None


def test_get_behind_master_no_upstream_tag(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
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

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    assert _get_behind_count() is None


def test_get_behind_fetch_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 1)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    assert _get_behind_count() is None


def test_get_behind_no_commits(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "fetch":
            return ("", 0)
        if args[0] == "rev-list":
            return ("0", 0)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    assert _get_behind_count() is None


# --- check ---


def test_check_skip_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SKIP_UPDATE_CHECK", "1")
    assert check() is False


def test_check_no_behind(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=None)
    assert check() is False


def test_check_user_says_no(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        "agent_wrap.commands.update._get_behind_count", return_value=("main", 2, "origin/main")
    )
    mocker.patch("builtins.input", return_value="n")
    mock_apply = mocker.patch("agent_wrap.commands.update.apply")
    assert check() is False
    mock_apply.assert_not_called()


def test_check_user_says_yes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        "agent_wrap.commands.update._get_behind_count", return_value=("main", 2, "origin/main")
    )
    mocker.patch("builtins.input", return_value="y")
    mock_apply = mocker.patch("agent_wrap.commands.update.apply")
    assert check() is True
    mock_apply.assert_called_once_with("origin/main")


def test_check_master_announces_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=("master", 3, "v1.1"))
    mocker.patch("builtins.input", return_value="y")
    mock_apply = mocker.patch("agent_wrap.commands.update.apply")
    assert check() is True
    mock_apply.assert_called_once_with("v1.1")
    assert "v1.1" in capsys.readouterr().out


def test_check_eof_error(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        "agent_wrap.commands.update._get_behind_count", return_value=("main", 2, "origin/main")
    )
    mocker.patch("builtins.input", side_effect=EOFError)
    assert check() is False


# --- _detect_claude_md_state ---


def test_detect_matches(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    default_md = ops_dir / "default-CLAUDE.md"
    content = "# hello"
    user_md.write_text(content)
    default_md.write_text(content)
    assert _detect_claude_md_state() == _MdState.MATCHES


def test_detect_customized(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    default_md = ops_dir / "default-CLAUDE.md"
    user_md.write_text("# user version")
    default_md.write_text("# default version")
    assert _detect_claude_md_state() == _MdState.CUSTOMIZED


def test_detect_missing(tmp_path: Path) -> None:
    assert _detect_claude_md_state() == _MdState.MISSING


# --- _handle_claude_md_propagation ---


def test_propagation_no_diff(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 0))
    result = _handle_claude_md_propagation("abc", "def", _MdState.MATCHES)
    assert result == _MdPropagation.UNCHANGED


def test_propagation_matches_deletes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# user content")
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    result = _handle_claude_md_propagation("abc", "def", _MdState.MATCHES)
    assert result == _MdPropagation.UPDATED
    assert not user_md.exists()


def test_propagation_customized_returns_conflict(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# custom")
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    result = _handle_claude_md_propagation("abc", "def", _MdState.CUSTOMIZED)
    assert result == _MdPropagation.CONFLICT
    assert user_md.exists()


# --- apply ---


def test_apply_cannot_determine_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    rc = apply()
    assert rc == 1
    captured = capsys.readouterr()
    assert "Update failed:" in captured.err
    assert "could not determine current branch" in captured.err


def test_apply_cannot_get_head(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mock_git = mocker.patch("agent_wrap.commands.update._git")
    mock_git.side_effect = [
        ("main", 0),  # symbolic-ref ok
        ("", 1),  # rev-parse HEAD fails
    ]
    rc = apply("origin/main")
    assert rc == 1
    captured = capsys.readouterr()
    assert "Update failed:" in captured.err
    assert "could not get current HEAD" in captured.err


def test_apply_merge_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    mock_full = mocker.patch(
        "agent_wrap.commands.update._git_full",
        return_value=("", 1, "fatal: not possible to fast-forward"),
    )
    rc = apply("origin/main")
    assert rc == 1
    out = capsys.readouterr().out
    assert "Update failed:" in out
    assert "fatal: not possible to fast-forward" in out
    # Fast-forwards to the resolved target ref, not a raw branch pull.
    assert mock_full.call_args.args == ("merge", "--ff-only", "origin/main")


def test_apply_merges_to_tag_target(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "symbolic-ref":
            return ("master", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    mock_full = mocker.patch("agent_wrap.commands.update._git_full", return_value=("", 0, ""))
    mocker.patch("subprocess.run").return_value.returncode = 0
    rc = apply("v1.1")
    assert rc == 0
    assert mock_full.call_args.args == ("merge", "--ff-only", "v1.1")


def test_apply_recomputes_target_when_none(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("master", 0))
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=None)
    rc = apply()
    assert rc == 0
    assert "Already up to date" in capsys.readouterr().out


def test_apply_already_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    mocker.patch("agent_wrap.commands.update._git_full", return_value=("", 0, ""))
    mocker.patch("subprocess.run").return_value.returncode = 0
    rc = apply("origin/main")
    assert rc == 0
    assert "Already up to date" in capsys.readouterr().out
