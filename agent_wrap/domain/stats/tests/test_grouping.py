# This file has been created with the assistance of an AI tool.
"""Tests for the `.agent_stats_leaf` transient-project grouping resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.constants import MARKER_NAME
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock


@pytest.fixture
def stats_svc(mocker: pytest_mock.MockFixture) -> StatsService:
    """Return a StatsService with a spec-mocked pricing dependency."""
    return StatsService(mocker.Mock(spec=PricingService), mocker.Mock(spec=ConfigService))


def _marker(directory: Path, contents: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MARKER_NAME).write_text(contents, encoding="utf-8")


def test_no_marker_keeps_project_standalone(tmp_path: Path, stats_svc: StatsService):
    proj = tmp_path / "solo"
    proj.mkdir()
    root, name, transient = stats_svc.resolve_group(proj)
    assert root == proj
    assert name == "solo"
    assert transient is False


@pytest.mark.parametrize("contents", ["", "   \n\n", "\n  batch-feb \nignored second line\n"])
def test_marker_content_is_ignored(tmp_path: Path, stats_svc: StatsService, contents: str):
    # The marker's content, if any, is never read — the group is always named
    # after the marker directory itself; `transient` reflects marker presence.
    runs = tmp_path / "runs"
    _marker(runs, contents)
    child = runs / "agent-xyz"
    child.mkdir()
    root, name, transient = stats_svc.resolve_group(child)
    assert root == runs
    assert name == "runs"
    assert transient is True


def test_marker_on_project_itself_is_found(tmp_path: Path, stats_svc: StatsService):
    proj = tmp_path / "proj"
    _marker(proj, "self-named")
    root, name, transient = stats_svc.resolve_group(proj)
    assert root == proj
    assert name == "proj"
    assert transient is True


def test_nearest_marker_wins_when_nested(tmp_path: Path, stats_svc: StatsService):
    outer = tmp_path / "outer"
    _marker(outer, "outer-group")
    inner = outer / "inner"
    _marker(inner, "inner-group")
    leaf = inner / "agent-1"
    leaf.mkdir()
    root, name, transient = stats_svc.resolve_group(leaf)
    assert root == inner
    assert name == "inner"
    assert transient is True


def test_two_projects_under_one_marker_share_a_group(tmp_path: Path, stats_svc: StatsService):
    runs = tmp_path / "runs"
    _marker(runs, "batch")
    a = runs / "a"
    b = runs / "b"
    a.mkdir()
    b.mkdir()
    root_a, name_a, _ = stats_svc.resolve_group(a)
    root_b, name_b, _ = stats_svc.resolve_group(b)
    assert root_a == root_b == runs
    assert name_a == name_b == "runs"


def test_symlinked_projects_group_by_literal_path(tmp_path: Path, stats_svc: StatsService):
    # Two physically-separate projects, symlinked into one marked common dir.
    # Grouping must follow the *literal* (symlinked) path so they aggregate,
    # even though their resolved physical locations differ.
    real_a = tmp_path / "real" / "alpha"
    real_b = tmp_path / "real" / "beta"
    real_a.mkdir(parents=True)
    real_b.mkdir(parents=True)

    common = tmp_path / "common"
    _marker(common, "fleet")
    (common / "alpha").symlink_to(real_a, target_is_directory=True)
    (common / "beta").symlink_to(real_b, target_is_directory=True)

    root_a, name_a, transient_a = stats_svc.resolve_group(common / "alpha")
    root_b, name_b, transient_b = stats_svc.resolve_group(common / "beta")

    # Both literal paths resolve to the same common group root and name...
    assert root_a == root_b == common
    assert name_a == name_b == "common"
    assert transient_a is transient_b is True
    # ...even though their physical (resolved) paths are distinct.
    assert real_a.resolve() != real_b.resolve()


def _central(tool_dir: Path, name: str) -> Path:
    """Create a central <hash> log dir under <tool_dir>/litellm-logs/."""
    d = tool_dir / LITELLM_LOGS_DIRNAME / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_orphaned_excludes_reachable_central_dir(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
) -> None:
    # A registered project whose .claude/litellm-logs symlink points at hashA must
    # exclude hashA; the unreferenced hashB is orphaned.
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    hash_a = _central(tmp_path, "hashA")
    _central(tmp_path, "hashB")

    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / LITELLM_LOGS_DIRNAME).symlink_to(hash_a, target_is_directory=True)

    orphaned = stats_svc.orphaned_log_dirs([project])
    assert orphaned == [tmp_path / LITELLM_LOGS_DIRNAME / "hashB"]


def test_orphaned_includes_all_when_no_projects(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
) -> None:
    # No registered projects → every central dir is orphaned (deleted projects).
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    _central(tmp_path, "hashA")
    _central(tmp_path, "hashB")

    orphaned = stats_svc.orphaned_log_dirs([])
    assert orphaned == [
        tmp_path / LITELLM_LOGS_DIRNAME / "hashA",
        tmp_path / LITELLM_LOGS_DIRNAME / "hashB",
    ]


def test_orphaned_empty_without_central_dir(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
) -> None:
    # No <tool_dir>/litellm-logs at all → nothing orphaned.
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    assert stats_svc.orphaned_log_dirs([]) == []


def test_orphaned_ignores_deleted_project_symlink(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    stats_svc: StatsService,
) -> None:
    # A registered project whose dir was deleted (no logs dir) cannot reach its
    # central hash, so that hash is orphaned.
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    _central(tmp_path, "hashA")
    gone = tmp_path / "deleted-proj"  # never created

    orphaned = stats_svc.orphaned_log_dirs([gone])
    assert orphaned == [tmp_path / LITELLM_LOGS_DIRNAME / "hashA"]
