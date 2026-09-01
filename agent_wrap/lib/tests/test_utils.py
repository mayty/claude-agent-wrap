# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.lib.utils."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.lib.path_hash import project_path_hash
from agent_wrap.lib.utils import (
    directory_size,
    generate_uuid,
    is_truthy_env,
    optional_truthy_env,
    sanitize_name,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_lowercase():
    assert sanitize_name("Hello") == "hello"


def test_replace_spaces():
    assert sanitize_name("hello world") == "hello-world"


def test_replace_special_chars():
    assert sanitize_name("hello@world!") == "hello-world"


def test_strip_leading_trailing_dashes():
    assert sanitize_name("---hello---") == "hello"


def test_preserve_dots_underscores_dashes():
    assert sanitize_name("my-project_v2.0") == "my-project_v2.0"


def test_empty_after_sanitize():
    assert sanitize_name("---") == ""


def test_mixed_case_and_special():
    assert sanitize_name("My Project (v2)") == "my-project--v2"


def test_returns_string():
    assert isinstance(generate_uuid(), str)


def test_format():
    parts = generate_uuid().split("-")
    assert len(parts) == 5


def test_lowercase_uuid():
    result = generate_uuid()
    assert result == result.lower()


def test_unique():
    assert generate_uuid() != generate_uuid()


def test_project_path_hash_is_16_hex(tmp_path: Path) -> None:
    result = project_path_hash(tmp_path)
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_project_path_hash_stable(tmp_path: Path) -> None:
    assert project_path_hash(tmp_path) == project_path_hash(tmp_path)


def test_project_path_hash_resolves_symlink_aliases(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real)
    # A symlink alias and its target resolve to the same path -> same hash.
    assert project_path_hash(link) == project_path_hash(real)


def test_project_path_hash_differs_by_path(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert project_path_hash(a) != project_path_hash(b)


@pytest.mark.parametrize("value", ["", "0", "false", "no", "FALSE", "NO", "False"])
def test_is_truthy_env_false(value: str) -> None:
    assert is_truthy_env(value) is False


@pytest.mark.parametrize("value", ["1", "yes", "YES", "true", "TRUE", "anything"])
def test_is_truthy_env_true(value: str) -> None:
    assert is_truthy_env(value) is True


def test_optional_truthy_env_none_when_unset() -> None:
    """An empty value reads as clearing the variable, not as asking for off."""
    assert optional_truthy_env("") is None


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", "NO", "False"])
def test_optional_truthy_env_false(value: str) -> None:
    assert optional_truthy_env(value) is False


@pytest.mark.parametrize("value", ["1", "yes", "YES", "true", "TRUE", "anything"])
def test_optional_truthy_env_true(value: str) -> None:
    assert optional_truthy_env(value) is True


_VANISHED = "vanished"


def test_directory_size_sums_nested_files(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "top").write_bytes(b"x" * 10)
    (tmp_path / "a" / "mid").write_bytes(b"x" * 20)
    (tmp_path / "a" / "b" / "deep").write_bytes(b"x" * 30)
    assert directory_size(tmp_path) == 60


def test_directory_size_empty_dir_is_zero(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert directory_size(empty) == 0


def test_directory_size_missing_dir_is_zero(tmp_path: Path) -> None:
    assert directory_size(tmp_path / "nope") == 0


def test_directory_size_ignores_directory_entries(tmp_path: Path) -> None:
    """Only regular files count — directory inodes have a nonzero st_size."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_bytes(b"x" * 5)
    assert directory_size(tmp_path) == 5


def test_directory_size_does_not_follow_symlinks(tmp_path: Path) -> None:
    """A symlink counts as itself, so a link out of the tree cannot inflate the sum."""
    outside = tmp_path / "outside"
    outside.write_bytes(b"x" * 10_000)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    assert directory_size(tree) < 10_000


def test_directory_size_tolerates_vanishing_file(tmp_path: Path, mocker: MockerFixture) -> None:
    """A file disappearing mid-walk shortens the total instead of aborting it."""
    (tmp_path / "keep").write_bytes(b"x" * 10)
    (tmp_path / "gone").write_bytes(b"x" * 999)
    real_stat = Path.stat

    def flaky(self: Path, *args: object, **kwargs: object):
        if self.name == "gone":
            raise OSError(_VANISHED)
        return real_stat(self, *args, **kwargs)  # pyrefly: ignore [bad-argument-type]

    mocker.patch.object(Path, "stat", flaky)
    assert directory_size(tmp_path) == 10


def test_directory_size_returns_zero_when_walk_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(Path, "rglob", side_effect=OSError("permission denied"))
    assert directory_size(tmp_path) == 0
