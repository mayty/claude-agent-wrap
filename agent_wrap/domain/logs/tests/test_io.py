# This file has been edited with the assistance of an AI tool.
"""
Tests for the log tree's shape: where a project's logs live, and who shares a group.

All that is left of this module's subject. Reading a session is
``agent_wrap/domain/logs/tests/test_stream.py``, and the session lists are
``test_listing.py`` -- both off the index rather than off these directories.
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.io import list_groups
from agent_wrap.domain.pricing.service import PricingService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest_mock
    from pytest_mock import MockerFixture

    from agent_wrap.domain.stats.service import StatsService
    from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository


@pytest.fixture
def isolated_stats(
    mocker: pytest_mock.MockFixture,
    tmp_path: Path,
    make_stats_service: Callable[[PricingService], StatsService],
) -> StatsService:
    """Return a StatsService with TOOL_DIR isolated from real filesystem."""
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    mocker.patch(
        "agent_wrap.domain.config.service.AGENT_LAUNCHES_DIR", tmp_path / ".agent-launches"
    )
    return make_stats_service(Mock(spec=PricingService))


@pytest.fixture
def config_svc(projects_repository: ProjectsRepository) -> ConfigService:
    """Return a real ConfigService for reading the project registry."""
    return ConfigService(
        display_service=Mock(spec=DisplayService), projects_repository=projects_repository
    )


def _ts_rec(iso: str, **extra: Any) -> dict[str, Any]:
    """Build a minimal record with a timing object whose start == end == iso."""
    epoch = datetime.fromisoformat(iso).timestamp()
    return {
        "timing": {"start": epoch, "completionStart": None, "end": epoch},
        "response": {},
        **extra,
    }


def _write_session(project: Path, provider: str, session_id: str, records: list[Any]) -> Path:
    sdir = project / ".claude" / "litellm-logs" / provider / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return sdir


def _write_central(tool_dir: Path, hash_name: str, session_id: str, records: list[Any]) -> Path:
    """Write a session directly under a central <hash> dir (no .claude wrapper)."""
    sdir = tool_dir / "litellm-logs" / hash_name / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return tool_dir / "litellm-logs" / hash_name


def test_unmarked_projects_stay_separate(
    tmp_path: Path,
    isolated_stats: StatsService,
    config_svc: ConfigService,
    register_projects: Callable[..., None],
) -> None:
    """Without a marker, each project remains its own entry (regression guard)."""
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])
    register_projects(a, b)

    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    assert {g["name"] for g in groups} == {"proj-a", "proj-b"}


def test_orphaned_group_is_appended_last(
    tmp_path: Path,
    isolated_stats: StatsService,
    config_svc: ConfigService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
) -> None:
    """Central log dirs with no registered project surface as an <orphaned> group."""
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)

    # Registered project symlinked to its central hashA dir.
    hash_a = _write_central(
        tool_dir, "hashA", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")]
    )
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(hash_a, target_is_directory=True)

    # Orphaned hashB — no project points at it.
    hash_b = _write_central(
        tool_dir, "hashB", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")]
    )

    register_projects(project)

    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    assert groups[-1]["name"] == "<orphaned>"
    # Its logs_dirs are the central <hash> dirs themselves, which is what lets the cache
    # take the hash off each of them and read the group's sessions out of the index.
    assert groups[-1]["logs_dirs"] == [hash_b]
    assert groups[-1]["paths"] == []


def test_no_orphaned_group_when_all_reachable(
    tmp_path: Path,
    isolated_stats: StatsService,
    config_svc: ConfigService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
) -> None:
    """No orphaned group is appended when every central dir is project-reachable."""
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    hash_a = _write_central(
        tool_dir, "hashA", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")]
    )
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(hash_a, target_is_directory=True)
    register_projects(project)

    raw_projects = config_svc.read_project_paths()
    assert all(g["name"] != "<orphaned>" for g in list_groups(isolated_stats, raw_projects))
