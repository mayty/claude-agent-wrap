# This file has been created with the assistance of an AI tool.
"""Tests for filesystem_type — naming the filesystem a path sits on."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

from agent_wrap.lib.mounts import filesystem_type

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


class Partition(NamedTuple):
    """The two fields of psutil's ``sdiskpart`` that :func:`filesystem_type` reads."""

    mountpoint: str
    fstype: str


MOUNT_TABLE = [
    Partition("/", "ext4"),
    Partition("/proc", "proc"),
    Partition("/mnt/c", "drvfs"),
    Partition("/mnt/share", "nfs4"),
    Partition("/mnt/c/nested", "ext4"),
]

REAL_PSEUDO_MOUNT = {
    "linux": (Path("/proc"), "proc"),
    "darwin": (Path("/dev"), "devfs"),
}


@pytest.fixture
def mount_table(mocker: MockerFixture) -> None:
    """Answer the mount-table query with a table we control."""
    mocker.patch("agent_wrap.lib.mounts.psutil.disk_partitions", return_value=MOUNT_TABLE)


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


def test_filesystem_type_is_none_without_a_mount_table(mocker: MockerFixture) -> None:
    """Off Linux there is no mount table to read, and 'unknown' is the honest answer."""
    mocker.patch("agent_wrap.lib.mounts.psutil.disk_partitions", side_effect=FileNotFoundError)
    assert filesystem_type(Path("/anywhere")) is None


def test_filesystem_type_reads_the_real_mount_table() -> None:
    """
    The one test with no mock in it: every other one asserts against its own fixture.

    ``all=True`` is what makes this pass -- both mounts named here are ones the default
    filter drops, because their device is a name rather than a path. That same filter
    drops the ``drvfs`` mount on a WSL host, which is the one the startup check exists
    to find. A platform with no entry fails loudly rather than skipping.
    """
    path, expected = REAL_PSEUDO_MOUNT[sys.platform]
    assert filesystem_type(path) == expected
