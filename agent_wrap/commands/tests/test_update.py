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
    _is_truthy_skip,
    apply,
    check,
)

# --- _is_truthy_skip ---


@pytest.mark.parametrize("value", ["", "0", "false", "no", "False", "NO"])
def test_truthy_skip_false(value: str) -> None:
    assert _is_truthy_skip(value) is False


@pytest.mark.parametrize("value", ["1", "yes", "true", "anything", "Yes", "TRUE"])
def test_truthy_skip_true(value: str) -> None:
    assert _is_truthy_skip(value) is True


# --- _get_behind_count ---


def test_get_behind_not_git_dir(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    assert _get_behind_count(tmp_path) is None


def test_get_behind_detached_head(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    def fake_git(*args, **kwargs):
        if args[0] == "rev-parse":
            return (".git", 0)
        if args[0] == "symbolic-ref":
            return ("", 1)
        return ("", 0)

    mocker.patch("agent_wrap.commands.update._git", side_effect=fake_git)
    assert _get_behind_count(tmp_path) is None


def test_get_behind_three_commits(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
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
    result = _get_behind_count(tmp_path)
    assert result == ("main", 3)


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
    assert _get_behind_count(tmp_path) is None


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
    assert _get_behind_count(tmp_path) is None


# --- check ---


def test_check_skip_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_AGENT_SKIP_UPDATE_CHECK", "1")
    assert check(tmp_path) is False


def test_check_no_behind(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=None)
    assert check(tmp_path) is False


def test_check_user_says_no(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=("main", 2))
    mocker.patch("builtins.input", return_value="n")
    mock_apply = mocker.patch("agent_wrap.commands.update.apply")
    assert check(tmp_path) is False
    mock_apply.assert_not_called()


def test_check_user_says_yes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=("main", 2))
    mocker.patch("builtins.input", return_value="y")
    mock_apply = mocker.patch("agent_wrap.commands.update.apply")
    assert check(tmp_path) is True
    mock_apply.assert_called_once_with(tmp_path)


def test_check_eof_error(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._get_behind_count", return_value=("main", 2))
    mocker.patch("builtins.input", side_effect=EOFError)
    assert check(tmp_path) is False


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
    assert _detect_claude_md_state(tmp_path) == "matches"


def test_detect_customized(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    default_md = ops_dir / "default-CLAUDE.md"
    user_md.write_text("# user version")
    default_md.write_text("# default version")
    assert _detect_claude_md_state(tmp_path) == "customized"


def test_detect_missing(tmp_path: Path) -> None:
    assert _detect_claude_md_state(tmp_path) == "missing"


# --- _handle_claude_md_propagation ---


def test_propagation_no_diff(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 0))
    _handle_claude_md_propagation(tmp_path, "abc", "def", "matches")
    # User file should not be touched


def test_propagation_matches_deletes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# user content")
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    _handle_claude_md_propagation(tmp_path, "abc", "def", "matches")
    assert not user_md.exists()


def test_propagation_customized_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    config_dir = tmp_path / ".claude_config" / ".claude"
    config_dir.mkdir(parents=True)
    user_md = config_dir / "CLAUDE.md"
    user_md.write_text("# custom")
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    _handle_claude_md_propagation(tmp_path, "abc", "def", "customized")
    assert "Warning" in capsys.readouterr().out
    assert user_md.exists()


# --- apply ---


def test_apply_cannot_determine_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.commands.update._git", return_value=("", 1))
    rc = apply(tmp_path)
    assert rc == 1
    assert "could not determine current branch" in capsys.readouterr().err


def test_apply_cannot_get_head(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mock_git = mocker.patch("agent_wrap.commands.update._git")
    mock_git.side_effect = [
        ("main", 0),  # symbolic-ref ok
        ("", 1),  # rev-parse HEAD fails
    ]
    rc = apply(tmp_path)
    assert rc == 1
    assert "could not get current HEAD" in capsys.readouterr().err


def test_apply_pull_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mock_git = mocker.patch("agent_wrap.commands.update._git")

    def fake_git(*args, **kwargs):
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        if args[0] == "pull":
            return ("", 1)
        return ("", 0)

    mock_git.side_effect = fake_git
    rc = apply(tmp_path)
    assert rc == 1
    assert "git pull failed" in capsys.readouterr().err


def test_apply_already_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mock_git = mocker.patch("agent_wrap.commands.update._git")

    def fake_git(*args, **kwargs):
        if args[0] == "symbolic-ref":
            return ("main", 0)
        if args[0] == "rev-parse":
            return ("abc123", 0)
        if args[0] == "pull":
            return ("", 0)
        return ("", 0)

    mock_git.side_effect = fake_git
    mocker.patch("subprocess.run").return_value.returncode = 0
    rc = apply(tmp_path)
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out
