from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.hash_resolver import load_strings
from agent_wrap.domain.logs.io import (
    lightweight_project_summary,
    list_groups,
    list_projects,
    list_sessions,
    logs_dir,
    projects_fingerprint,
    read_last_record_ts,
    read_meta_json,
    read_session,
    read_strings,
    scan_session_meta,
    session_fingerprint,
    sessions_fingerprint,
    write_meta_json,
)
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock
    from pytest_mock import MockerFixture


@pytest.fixture
def stats_svc() -> StatsService:
    """Return a StatsService with a no-op pricing service."""
    return StatsService(
        pricing_service=Mock(spec=PricingService), config_service=Mock(spec=ConfigService)
    )


@pytest.fixture
def isolated_stats(mocker: pytest_mock.MockFixture, tmp_path: Path) -> StatsService:
    """Return a StatsService with TOOL_DIR isolated from real filesystem."""
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    mocker.patch("agent_wrap.domain.logs.io.AGENT_LAUNCHES_DIR", tmp_path / ".agent-launches")
    mocker.patch(
        "agent_wrap.domain.config.service.AGENT_LAUNCHES_DIR", tmp_path / ".agent-launches"
    )
    return StatsService(
        pricing_service=Mock(spec=PricingService), config_service=Mock(spec=ConfigService)
    )


@pytest.fixture
def config_svc() -> ConfigService:
    """Return a real ConfigService for reading the project registry."""
    return ConfigService(display_service=Mock(spec=DisplayService))


# ---------------------------------------------------------------------------
# Helpers (data factories)
# ---------------------------------------------------------------------------


def _epoch(iso: str) -> float:
    """ISO-8601 string -> Unix epoch seconds."""
    return datetime.fromisoformat(iso).timestamp()


def _ts_rec(iso: str, **extra: Any) -> dict[str, Any]:
    """Build a minimal record with a timing object whose start == end == iso."""
    e = _epoch(iso)
    return {"timing": {"start": e, "completionStart": None, "end": e}, "response": {}, **extra}


if TYPE_CHECKING:
    from agent_wrap.domain.providers.litellm_common.models import LogRecord

# --- filesystem helpers ---


def _write_session(project: Path, provider: str, session_id: str, records: list[Any]) -> Path:
    sdir = project / ".claude" / "litellm-logs" / provider / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return sdir


def test_list_sessions_enumerates_and_sorts(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "sess-old",
        [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")],
    )
    _write_session(
        project,
        "litellm-bedrock",
        "sess-new",
        [
            _ts_rec("2026-06-05T00:00:00+00:00", model="m/b"),
            _ts_rec("2026-06-05T01:00:00+00:00", model="m/b"),
        ],
    )
    sessions = list_sessions(project)
    assert [s["session_id"] for s in sessions] == ["sess-new", "sess-old"]
    assert sessions[0]["count"] == 2
    assert sessions[0]["models"] == ["b"]


def test_list_sessions_skips_empty_and_missing(tmp_path: Path):
    project = tmp_path / "proj"
    # Directory with no messages.jsonl.
    (project / ".claude" / "litellm-logs" / "litellm-bedrock" / "empty").mkdir(parents=True)
    assert list_sessions(project) == []


def test_list_sessions_derives_alias_from_naming_record(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [
            _ts_rec("2026-06-05T00:00:00+00:00", model="m/a"),
            _naming_record('{"name": "derived-slug"}'),
        ],
    )
    assert list_sessions(project)[0]["alias"] == "derived-slug"


def test_list_sessions_meta_json_alias_used(tmp_path: Path):
    """Alias from meta.json cache is used when the cache is fresh."""
    project = tmp_path / "proj"
    sdir = _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_naming_record('{"name": "derived-slug"}')],
    )
    _write_meta_file(
        sdir,
        {
            "count": 1,
            "last_ts": _epoch("2026-06-05T00:00:00+00:00"),
            "models": ["a"],
            "alias": "meta-slug",
        },
    )
    assert list_sessions(project)[0]["alias"] == "meta-slug"


def test_list_sessions_alias_none_when_absent(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    assert list_sessions(project)[0]["alias"] is None


def test_read_session_normalizes_and_resolves(tmp_path: Path, mocker: MockerFixture):
    project = tmp_path / "proj"
    sdir = _write_session(project, "litellm-bedrock", "s1", [_raw_record()])
    (sdir / "strings.jsonl").write_text(
        json.dumps({"hash": "hash:s", "original": "X"}) + "\n", encoding="utf-8"
    )
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value.compute_cost.return_value = None  # type: ignore[implicit-any-empty-container]
    pricing = PricingService(provider_service=mock_ps, display_service=Mock(spec=DisplayService))
    data = read_session(project, "s1", pricing=pricing)
    assert data["session_meta"] is not None
    assert data["session_meta"]["session_id"] == "s1"
    # Records are returned unresolved — the strings.jsonl mapping exists but is
    # not applied to records (hash resolution moved to the frontend).
    assert len(data["reqs"]) == 1


def test_read_session_from_index(tmp_path: Path, mocker: MockerFixture):
    """from_index slices records but session_meta still reflects the full count."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [
            _ts_rec("2026-06-01T00:00:00+00:00", model="m/a"),
            _ts_rec("2026-06-05T00:00:00+00:00", model="m/b"),
            _ts_rec("2026-06-05T01:00:00+00:00", model="m/c"),
        ],
    )
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value.compute_cost.return_value = None  # type: ignore[implicit-any-empty-container]
    pricing = PricingService(provider_service=mock_ps, display_service=Mock(spec=DisplayService))
    data = read_session(project, "s1", pricing=pricing, from_index=1)
    assert data["session_meta"] is not None
    assert data["session_meta"]["count"] == 3
    assert len(data["reqs"]) == 2


def test_read_session_from_index_beyond(tmp_path: Path, mocker: MockerFixture):
    """from_index beyond total records returns empty reqs but full session_meta."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value.compute_cost.return_value = None  # type: ignore[implicit-any-empty-container]
    pricing = PricingService(provider_service=mock_ps, display_service=Mock(spec=DisplayService))
    data = read_session(project, "s1", pricing=pricing, from_index=99)
    assert data["session_meta"] is not None
    assert data["session_meta"]["count"] == 1
    assert len(data["reqs"]) == 0


def test_session_fingerprint_reflects_file(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    fp = session_fingerprint(project, "s1")
    assert isinstance(fp["mtime"], int)
    assert isinstance(fp["size"], int)
    assert fp["size"] > 0


def test_session_fingerprint_null_when_missing(tmp_path: Path):
    project = tmp_path / "proj"
    assert session_fingerprint(project, "nope") == {
        "mtime": None,
        "size": None,
    }


def test_list_sessions_merges_across_providers(tmp_path: Path):
    """Same session_id under two providers → one merged entry."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")],
    )
    _write_session(
        project,
        "litellm-deepseek",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")],
    )
    sessions = list_sessions(project)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "s1"
    assert s["providers"] == ["litellm-bedrock", "litellm-deepseek"]
    assert s["count"] == 2
    assert s["models"] == ["a", "b"]
    assert s["first_ts"] == _epoch("2026-06-01T00:00:00+00:00")
    assert s["last_ts"] == _epoch("2026-06-05T00:00:00+00:00")


def test_list_sessions_providers_field_shape(tmp_path: Path):
    """Single-provider sessions still have a providers list (of length 1)."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sessions = list_sessions(project)
    assert sessions[0]["providers"] == ["litellm-bedrock"]
    assert "provider" not in sessions[0]


def test_list_sessions_dedupes_provider_on_session_id_collision(tmp_path: Path) -> None:
    """Two unrelated logs dirs sharing a session_id and provider merge without a duplicate badge."""
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session(
        a,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")],
    )
    _write_session(
        b,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")],
    )
    sessions = list_sessions([logs_dir(a), logs_dir(b)])
    assert len(sessions) == 1
    assert sessions[0]["providers"] == ["litellm-bedrock"]
    assert sessions[0]["count"] == 2


def test_read_session_merges_across_providers(tmp_path: Path, mocker: MockerFixture):
    """Records from two providers are interleaved by timestamp."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [
            {
                "timing": {
                    "start": _epoch("2026-06-01T00:00:00+00:00"),
                    "completionStart": None,
                    "end": _epoch("2026-06-01T00:00:00+00:00"),
                },
                "status": "success",
                "model": "m/a",
                "request": {
                    "body": {"data": {"messages": [{"role": "user", "content": "from bedrock"}]}}
                },
                "response": {"choices": [{"message": {"content": "bedrock reply"}}]},
            },
        ],
    )
    _write_session(
        project,
        "litellm-deepseek",
        "s1",
        [
            {
                "timing": {
                    "start": _epoch("2026-06-01T00:01:00+00:00"),
                    "completionStart": None,
                    "end": _epoch("2026-06-01T00:01:00+00:00"),
                },
                "status": "success",
                "model": "m/b",
                "request": {
                    "body": {"data": {"messages": [{"role": "user", "content": "from deepseek"}]}}
                },
                "response": {"choices": [{"message": {"content": "deepseek reply"}}]},
            },
        ],
    )
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value.compute_cost.return_value = None  # type: ignore[implicit-any-empty-container]
    pricing = PricingService(provider_service=mock_ps, display_service=Mock(spec=DisplayService))
    data = read_session(project, "s1", pricing=pricing)
    reqs = data["reqs"]
    assert len(reqs) == 2
    assert reqs[0]["messages"] == [{"role": "user", "content": "from bedrock"}]
    assert reqs[1]["messages"] == [{"role": "user", "content": "from deepseek"}]
    # session_meta should be merged across providers
    sm = data["session_meta"]
    assert sm is not None
    assert sm["session_id"] == "s1"
    assert sm["providers"] == ["litellm-bedrock", "litellm-deepseek"]
    assert sm["count"] == 2
    assert sm["models"] == ["a", "b"]


def test_read_strings_concatenates(tmp_path: Path):
    """read_strings returns the raw strings.jsonl content concatenated."""
    project = tmp_path / "proj"
    sdir = _write_session(project, "litellm-bedrock", "s1", [_raw_record()])
    (sdir / "strings.jsonl").write_text(
        '{"hash": "hash:a", "original": "AAA"}\n{"hash": "hash:b", "original": "BBB"}\n',
        encoding="utf-8",
    )
    result = read_strings(project, "s1")
    assert "hash:a" in result
    assert "AAA" in result
    assert "hash:b" in result
    assert "BBB" in result


def test_read_strings_empty_when_no_file(tmp_path: Path):
    """read_strings returns an empty string when no strings.jsonl exists."""
    project = tmp_path / "proj"
    _write_session(project, "litellm-bedrock", "s1", [_raw_record()])
    assert read_strings(project, "s1") == ""


def test_session_fingerprint_combines_across_providers(tmp_path: Path):
    """Fingerprint reflects max mtime and combined size across providers."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    _write_session(
        project,
        "litellm-deepseek",
        "s1",
        [
            _ts_rec("2026-06-05T00:00:00+00:00", model="m/b"),
            _ts_rec("2026-06-05T01:00:00+00:00", model="m/b"),
        ],
    )
    fp = session_fingerprint(project, "s1")
    assert isinstance(fp["mtime"], int)
    assert isinstance(fp["size"], int)
    # Size should be at least the sum of both files (each file > 0 bytes).
    size_bedrock = (
        (project / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1" / "messages.jsonl")
        .stat()
        .st_size
    )
    size_deepseek = (
        (project / ".claude" / "litellm-logs" / "litellm-deepseek" / "s1" / "messages.jsonl")
        .stat()
        .st_size
    )
    assert fp["size"] == size_bedrock + size_deepseek


def test_sessions_fingerprint_reflects_changes(tmp_path: Path):
    """Fingerprint changes when a record is appended to any session."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    fp1 = sessions_fingerprint(project)
    assert isinstance(fp1["mtime"], int)
    assert isinstance(fp1["size"], int)
    assert fp1["size"] > 0

    # Append a record — mtime and size should change.
    _write_session(
        project,
        "litellm-bedrock",
        "s2",
        [_ts_rec("2026-06-05T01:00:00+00:00", model="m/b")],
    )
    fp2 = sessions_fingerprint(project)
    assert fp2["mtime"] != fp1["mtime"] or fp2["size"] != fp1["size"]


def test_sessions_fingerprint_null_when_empty(tmp_path: Path):
    """No sessions at all → null fingerprint."""
    project = tmp_path / "proj"
    assert sessions_fingerprint(project) == {"mtime": None, "size": None}


def test_load_strings_round_trip(tmp_path: Path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "strings.jsonl").write_text(
        json.dumps({"hash": "hash:a", "original": "A"})
        + "\n"
        + "not json\n"
        + json.dumps({"hash": "hash:b", "original": "B"})
        + "\n",
        encoding="utf-8",
    )
    assert load_strings(sdir) == {"hash:a": "A", "hash:b": "B"}


def test_list_projects_filters_to_those_with_logs(
    tmp_path: Path, isolated_stats: StatsService, config_svc: ConfigService
) -> None:
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    with_logs = tmp_path / "with"
    without_logs = tmp_path / "without"
    _write_session(
        with_logs,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    (tool_dir / ".agent-launches" / "projects.txt").write_text(
        f"{with_logs}\n{without_logs}\n", encoding="utf-8"
    )
    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    result = list_projects(groups)
    assert [p["path"] for p in result] == [str(with_logs)]
    assert result[0]["sessions"] == 1
    assert result[0]["id"] == 0


def test_list_projects_empty_without_registry(
    isolated_stats: StatsService, config_svc: ConfigService
) -> None:
    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    assert list_projects(groups) == []


# --- .agent_stats_leaf grouping --------------------------------------------


def test_list_projects_aggregates_marked_group(
    tmp_path: Path, isolated_stats: StatsService, config_svc: ConfigService
) -> None:
    """Two projects under a .agent_stats_leaf marker collapse to one entry."""
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".agent_stats_leaf").write_text("batch-feb\n", encoding="utf-8")

    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{a}\n{b}\n", encoding="utf-8")

    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    result = list_projects(groups)
    assert len(result) == 1
    p = result[0]
    assert p["name"] == "batch-feb"
    assert p["path"] == str(runs)
    assert p["sessions"] == 2
    assert p["last_ts"] == _epoch("2026-06-05T00:00:00+00:00")


def test_list_sessions_unions_group_members(tmp_path: Path) -> None:
    """list_sessions over a list of logs dirs merges sessions from every member."""
    runs = tmp_path / "runs"
    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])

    sessions = list_sessions([logs_dir(a), logs_dir(b)])
    assert [s["session_id"] for s in sessions] == ["s2", "s1"]


def test_unmarked_projects_stay_separate(
    tmp_path: Path, isolated_stats: StatsService, config_svc: ConfigService
) -> None:
    """Without a marker, each project remains its own entry (regression guard)."""
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{a}\n{b}\n", encoding="utf-8")

    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    result = list_projects(groups)
    assert {p["name"] for p in result} == {"proj-a", "proj-b"}


# --- <orphaned> synthetic group --------------------------------------------


def _write_central(tool_dir: Path, hash_name: str, session_id: str, records: list[Any]) -> Path:
    """Write a session directly under a central <hash> dir (no .claude wrapper)."""
    sdir = tool_dir / "litellm-logs" / hash_name / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return tool_dir / "litellm-logs" / hash_name


def test_orphaned_group_exposed_and_readable(
    tmp_path: Path, isolated_stats: StatsService, config_svc: ConfigService, mocker: MockerFixture
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

    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    # The orphaned group is appended last with the synthetic name.
    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    assert groups[-1]["name"] == "<orphaned>"
    assert groups[-1]["logs_dirs"] == [hash_b]

    result = list_projects(groups)
    assert "<orphaned>" in {p["name"] for p in result}
    orphaned = next(p for p in result if p["name"] == "<orphaned>")
    assert orphaned["sessions"] == 1

    # Resolving the group id yields the central dirs; reading them returns s2.
    assert 0 <= orphaned["id"] < len(groups)
    assert groups[orphaned["id"]]["logs_dirs"] == [hash_b]
    assert [s["session_id"] for s in list_sessions(groups[orphaned["id"]]["logs_dirs"])] == ["s2"]


def test_no_orphaned_group_when_all_reachable(
    tmp_path: Path, isolated_stats: StatsService, config_svc: ConfigService, mocker: MockerFixture
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
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    raw_projects = config_svc.read_project_paths()
    assert all(g["name"] != "<orphaned>" for g in list_groups(isolated_stats, raw_projects))


# --- read_last_record_ts ---


def test_read_last_record_ts_returns_last_ts(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        json.dumps(_ts_rec("2026-06-01T00:00:00+00:00"))
        + "\n"
        + json.dumps(_ts_rec("2026-06-05T12:00:00+00:00"))
        + "\n",
        encoding="utf-8",
    )
    assert read_last_record_ts(f) == _epoch("2026-06-05T12:00:00+00:00")


def test_read_last_record_ts_returns_none_for_empty_file(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text("", encoding="utf-8")
    assert read_last_record_ts(f) is None


def test_read_last_record_ts_returns_none_for_missing_file(tmp_path: Path):
    assert read_last_record_ts(tmp_path / "nope.jsonl") is None


def test_read_last_record_ts_handles_single_record(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    assert read_last_record_ts(f) == _epoch("2026-06-05T00:00:00+00:00")


def test_read_last_record_ts_handles_no_trailing_newline(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")),
        encoding="utf-8",
    )
    assert read_last_record_ts(f) == _epoch("2026-06-05T00:00:00+00:00")


def test_read_last_record_ts_handles_multibyte_utf8_content(tmp_path: Path):
    """
    Multibyte UTF-8 characters within records must not prevent
    extracting the ``timing.end`` field from the last valid JSON line.
    """
    f = tmp_path / "messages.jsonl"
    # Records containing 3-byte UTF-8 characters (Unicode Hiragana).
    records = [
        json.dumps(_ts_rec("2026-06-01T00:00:00+00:00", data="あいうえお")),
        json.dumps(_ts_rec("2026-06-05T12:00:00+00:00", data="かきくけこ")),
    ]
    f.write_text("\n".join(records) + "\n", encoding="utf-8")
    assert read_last_record_ts(f) == _epoch("2026-06-05T12:00:00+00:00")


def test_read_last_record_ts_handles_non_json_lines(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        "not json\n" + json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    assert read_last_record_ts(f) == _epoch("2026-06-05T00:00:00+00:00")


# --- lightweight_project_summary ---


def test_lightweight_project_summary_empty_project(tmp_path: Path):
    project = tmp_path / "proj"
    assert lightweight_project_summary(project) == (0, None)


def test_lightweight_project_summary_single_session(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    count, last_ts = lightweight_project_summary(project)
    assert count == 1
    assert last_ts == _epoch("2026-06-05T00:00:00+00:00")


def test_lightweight_project_summary_multiple_sessions(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s-old",
        [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")],
    )
    _write_session(
        project,
        "litellm-bedrock",
        "s-new",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")],
    )
    count, last_ts = lightweight_project_summary(project)
    assert count == 2
    assert last_ts == _epoch("2026-06-05T00:00:00+00:00")


def test_lightweight_project_summary_dedups_across_providers(tmp_path: Path):
    """Same session_id under two providers → count=1, max ts from newest file."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")],
    )
    # Ensure the second write gets a strictly higher mtime so the
    # function picks the correct file for last_ts extraction.
    time.sleep(0.01)
    _write_session(
        project,
        "litellm-deepseek",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")],
    )
    count, last_ts = lightweight_project_summary(project)
    assert count == 1
    assert last_ts == _epoch("2026-06-05T00:00:00+00:00")


def test_lightweight_project_summary_skips_empty_sessions(tmp_path: Path):
    """Session dir without a messages.jsonl should be skipped."""
    project = tmp_path / "proj"
    (project / ".claude" / "litellm-logs" / "litellm-bedrock" / "empty").mkdir(parents=True)
    count, last_ts = lightweight_project_summary(project)
    assert count == 0
    assert last_ts is None


def test_list_projects_lightweight_produces_same_shape(
    tmp_path: Path, isolated_stats: StatsService, config_svc: ConfigService
) -> None:
    """Output dict must have the same keys as before the optimization."""
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{project}\n", encoding="utf-8")
    raw_projects = config_svc.read_project_paths()
    groups = list_groups(isolated_stats, raw_projects)
    result = list_projects(groups)
    assert len(result) == 1
    p = result[0]
    assert set(p.keys()) == {"id", "path", "name", "sessions", "last_ts"}
    assert p["sessions"] == 1
    assert p["last_ts"] == _epoch("2026-06-05T00:00:00+00:00")


@pytest.mark.usefixtures("isolated_stats")
def test_projects_fingerprint_reflects_changes(
    tmp_path: Path,
    config_svc: ConfigService,
) -> None:
    """Fingerprint changes when a record is appended anywhere across projects."""
    tool_dir = tmp_path
    (tool_dir / ".agent-launches").mkdir(parents=True)
    project = tmp_path / "proj"
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{project}\n", encoding="utf-8")
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    raw_projects = config_svc.read_project_paths()
    fp1 = projects_fingerprint(raw_projects)
    assert isinstance(fp1["mtime"], int)
    assert isinstance(fp1["size"], int)
    assert fp1["size"] > 0

    # Append a record in a second session — fingerprint should change.
    _write_session(
        project,
        "litellm-bedrock",
        "s2",
        [_ts_rec("2026-06-05T01:00:00+00:00", model="m/b")],
    )
    raw_projects = config_svc.read_project_paths()
    fp2 = projects_fingerprint(raw_projects)
    assert fp2["mtime"] != fp1["mtime"] or fp2["size"] != fp1["size"]


@pytest.mark.usefixtures("isolated_stats")
def test_projects_fingerprint_null_when_no_registry(
    config_svc: ConfigService,
) -> None:
    """No registry file → null fingerprint."""
    raw_projects = config_svc.read_project_paths()
    assert projects_fingerprint(raw_projects) == {"mtime": None, "size": None}


# --- meta.json caching (read_meta_json / write_meta_json / scan_session_meta) ---


def _write_meta_file(session_dir: Path, meta: dict[str, Any]) -> Path:
    """Write a meta.json file directly (bypassing atomic write)."""
    f = session_dir / "meta.json"
    f.write_text(json.dumps(meta), encoding="utf-8")
    return f


def test_read_meta_json_returns_dict_when_fresh(tmp_path: Path):
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    # Write meta.json AFTER messages.jsonl so it's fresher.
    _write_meta_file(
        sdir, {"count": 1, "last_ts": _epoch("2026-06-05T00:00:00+00:00"), "models": ["a"]}
    )
    cached = read_meta_json(sdir)
    assert cached is not None
    assert cached["count"] == 1
    assert cached["models"] == ["a"]


def test_read_meta_json_returns_none_when_missing(tmp_path: Path):
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    assert read_meta_json(sdir) is None


def test_read_meta_json_returns_none_when_stale(tmp_path: Path):
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    # Write meta.json BEFORE messages.jsonl, making it stale.
    _write_meta_file(sdir, {"count": 0, "last_ts": None, "models": []})
    # Force meta.json mtime into the past so messages.jsonl is strictly newer.
    past = time.time() - 60
    os.utime(sdir / "meta.json", (past, past))
    # Append another record to messages.jsonl to make it newer.
    msg_file = sdir / "messages.jsonl"
    with msg_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_ts_rec("2026-06-06T00:00:00+00:00", model="m/b")) + "\n")
    assert read_meta_json(sdir) is None


def test_read_meta_json_returns_none_when_corrupt(tmp_path: Path):
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    (sdir / "meta.json").write_text("not valid json", encoding="utf-8")
    # Ensure meta.json mtime >= messages.jsonl mtime for the freshness check.
    time.sleep(0.01)
    # Touch meta.json so it passes the staleness check but fails JSON parse.
    (sdir / "meta.json").write_text("still not json {{{", encoding="utf-8")
    assert read_meta_json(sdir) is None


def test_read_meta_json_returns_none_for_legacy_string_last_ts(tmp_path: Path):
    """A pre-timing-format cache (ISO-string last_ts) is treated as stale."""
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    # Old sentinel/ISO string would crash the float-keyed session sort.
    _write_meta_file(sdir, {"count": 1, "last_ts": "0000-00-00T00:00:00+00:00", "models": ["a"]})
    assert read_meta_json(sdir) is None


def test_scan_session_meta_uses_cache(tmp_path: Path):
    """When meta.json is fresh, scan_session_meta returns cached data."""
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    _write_meta_file(
        sdir,
        {
            "count": 5,
            "last_ts": _epoch("2026-06-06T00:00:00+00:00"),
            "models": ["a", "b"],
            "alias": "cached-alias",
            "title": "Cached Title",
        },
    )
    result = scan_session_meta(sdir, "litellm-bedrock")
    assert result is not None
    assert result["count"] == 5
    assert result["last_ts"] == _epoch("2026-06-06T00:00:00+00:00")
    assert result["models"] == ["a", "b"]
    assert result["alias"] == "cached-alias"
    assert result["title"] == "Cached Title"
    assert result["provider"] == "litellm-bedrock"
    assert result["session_id"] == "s1"


def test_scan_session_meta_falls_back_without_cache(tmp_path: Path):
    """Without meta.json, scan_session_meta scans messages.jsonl and seeds cache."""
    _write_session(
        tmp_path,
        "litellm-bedrock",
        "s1",
        [
            _ts_rec("2026-06-05T00:00:00+00:00", model="m/a"),
            _ts_rec("2026-06-05T01:00:00+00:00", model="m/b"),
        ],
    )
    sdir = tmp_path / ".claude" / "litellm-logs" / "litellm-bedrock" / "s1"
    # No meta.json — should fall back to scan.
    result = scan_session_meta(sdir, "litellm-bedrock")
    assert result is not None
    assert result["count"] == 2
    assert result["models"] == ["a", "b"]
    # Should have seeded the cache for next time.
    cached = read_meta_json(sdir)
    assert cached is not None
    assert cached["count"] == 2
    assert cached["models"] == ["a", "b"]


def test_write_andread_meta_json_round_trip(tmp_path: Path):
    """write_meta_json produces a file that read_meta_json can consume."""
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "messages.jsonl").write_text(
        json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    write_meta_json(
        sdir,
        {
            "count": 3,
            "last_ts": _epoch("2026-06-05T02:00:00+00:00"),
            "models": ["x", "y"],
            "alias": "test-alias",
            "title": "Test Title",
        },
    )
    cached = read_meta_json(sdir)
    assert cached is not None
    assert cached["count"] == 3
    assert cached["last_ts"] == _epoch("2026-06-05T02:00:00+00:00")
    assert cached["models"] == ["x", "y"]
    assert cached["alias"] == "test-alias"
    assert cached["title"] == "Test Title"


# --- helpers shared with normalize tests ---


def _raw_record() -> Any:
    return cast(
        "LogRecord",
        {
            "timing": {
                "start": _epoch("2026-06-05T12:00:00+00:00"),
                "completionStart": None,
                "end": _epoch("2026-06-05T12:00:01+00:00"),
            },
            "status": "success",
            "model": "us.anthropic.claude-opus-4-8",
            "request": {
                "body": {
                    "data": {
                        "messages": [{"role": "user", "content": "hello"}],
                        "system": "be brief",
                        "tools": [{"name": "Read"}],
                    }
                }
            },
            "response": {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
            "error": None,
        },
    )


def _naming_record(content: str) -> Any:
    return cast(
        "LogRecord",
        {
            "timing": {
                "start": _epoch("2026-06-05T12:00:00+00:00"),
                "completionStart": None,
                "end": _epoch("2026-06-05T12:00:01+00:00"),
            },
            "status": "success",
            "model": "m",
            "request": {},
            "response": {"choices": [{"message": {"role": "assistant", "content": content}}]},
            "error": None,
        },
    )
