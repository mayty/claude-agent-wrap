# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.lib.utils."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.lib.path_hash import project_path_hash
from agent_wrap.lib.utils import generate_uuid, is_truthy_env, sanitize_name

if TYPE_CHECKING:
    from pathlib import Path


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


# --- project_path_hash ---


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


# --- is_truthy_env ---


@pytest.mark.parametrize("value", ["", "0", "false", "no", "FALSE", "NO", "False"])
def test_is_truthy_env_false(value: str) -> None:
    assert is_truthy_env(value) is False


@pytest.mark.parametrize("value", ["1", "yes", "YES", "true", "TRUE", "anything"])
def test_is_truthy_env_true(value: str) -> None:
    assert is_truthy_env(value) is True
