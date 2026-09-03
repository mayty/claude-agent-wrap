# This file has been created with the assistance of an AI tool.
"""Tests for filesystem_type — reading a path's filesystem out of /proc/self/mounts."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import agent_wrap.lib.mounts as mounts_mod
from agent_wrap.lib.mounts import filesystem_type

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

MOUNT_TABLE = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid 0 0
C:\\134 /mnt/c drvfs rw,noatime 0 0
server:/export /mnt/share nfs4 rw,relatime 0 0
/dev/sdb1 /mnt/c/nested ext4 rw,relatime 0 0
"""


@pytest.fixture
def mount_table(tmp_path: Path, mocker: MockerFixture) -> None:
    """Point the module at a table we control, where the module names it."""
    table = tmp_path / "mounts"
    table.write_text(MOUNT_TABLE, encoding="utf-8")
    mocker.patch.object(mounts_mod, "MOUNTS_FILE", table)


@pytest.mark.usefixtures("mount_table")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("/"), "ext4"),
        (Path("/home/someone/code"), "ext4"),
        (Path("/mnt/c"), "drvfs"),
        (Path("/mnt/c/Users/me/project"), "drvfs"),
        (Path("/mnt/share/logs"), "nfs4"),
    ],
)
def test_filesystem_type_reports_the_containing_mount(path: Path, expected: str) -> None:
    assert filesystem_type(path) == expected


@pytest.mark.usefixtures("mount_table")
def test_filesystem_type_prefers_the_longest_matching_mount() -> None:
    """
    Mounts nest, so the deepest match is the only correct one.

    `/` matches every path; picking any shorter match would report the parent
    filesystem for every nested mount on the system.
    """
    assert filesystem_type(Path("/mnt/c/nested/thing")) == "ext4"


def test_filesystem_type_decodes_an_escaped_mount_point(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """The kernel octal-escapes spaces, so an unescaped compare would never match."""
    table = tmp_path / "mounts"
    table.write_text("/dev/sda1 /media/my\\040drive vfat rw 0 0\n", encoding="utf-8")
    mocker.patch.object(mounts_mod, "MOUNTS_FILE", table)
    assert filesystem_type(Path("/media/my drive/file")) == "vfat"


def test_filesystem_type_is_none_without_a_mount_table(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Off Linux there is no /proc/self/mounts, and 'unknown' is the honest answer."""
    mocker.patch.object(mounts_mod, "MOUNTS_FILE", tmp_path / "absent")
    assert filesystem_type(Path("/anywhere")) is None


def test_filesystem_type_skips_malformed_lines(tmp_path: Path, mocker: MockerFixture) -> None:
    table = tmp_path / "mounts"
    table.write_text("garbage\n\n/dev/sda1 / ext4 rw 0 0\n", encoding="utf-8")
    mocker.patch.object(mounts_mod, "MOUNTS_FILE", table)
    assert filesystem_type(Path("/whatever")) == "ext4"
