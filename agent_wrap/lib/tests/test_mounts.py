# This file has been created with the assistance of an AI tool.
"""Tests for filesystem_type — naming the filesystem a path sits on."""

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

    ``all=True`` is what makes this pass -- the default filters to physical devices, and
    on a WSL host that drops exactly the ``drvfs`` mount the startup check looks for.
    """
    assert filesystem_type(Path("/proc")) == "proc"
