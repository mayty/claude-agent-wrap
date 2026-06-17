# This file has been edited with the assistance of an AI tool.
"""Tests for the `logs` subcommand's data access and record normalization."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.commands import logs as logs_mod
from agent_wrap.commands.logs import (
    _lightweight_project_summary,
    _parse_port,
    _pid_alive,
    _read_last_record_ts,
    _read_meta_json,
    _read_state,
    _resolve_hashes,
    _running_server,
    _scan_session_meta,
    _state_file,
    _stop,
    _write_meta_json,
    _write_state,
    extract_alias,
    list_projects,
    list_sessions,
    load_strings,
    normalize_record,
    projects_fingerprint,
    read_session,
    resolve_static,
    run,
    session_fingerprint,
    sessions_fingerprint,
)

if TYPE_CHECKING:
    from agent_wrap.providers.litellm_common.callback import LogRecord

if TYPE_CHECKING:
    from pathlib import Path

# --- timing helpers ---------------------------------------------------------
# Records now carry a `timing` object of Unix epoch-seconds floats rather than
# the old ISO `ts`/`end_ts` strings. These helpers keep the test fixtures
# readable (ISO in, epoch out) while matching the new on-disk shape.


def _epoch(iso: str) -> float:
    """ISO-8601 string -> Unix epoch seconds (matches the migration/callback)."""
    return datetime.fromisoformat(iso).timestamp()


def _ts_rec(iso: str, **extra: Any) -> dict[str, Any]:
    """Build a minimal record with a `timing` object whose start == end == ``iso``."""
    e = _epoch(iso)
    return {"timing": {"start": e, "completionStart": None, "end": e}, **extra}


# --- normalize_record ---


def _raw_record() -> LogRecord:
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


def test_normalize_pulls_real_request_data():
    out = normalize_record(_raw_record(), {})
    assert out["messages"] == [{"role": "user", "content": "hello"}]
    assert out["system"] == "be brief"
    assert out["tools"] == [{"name": "Read"}]


def test_normalize_falls_back_to_body_when_data_is_not_dict():
    # Simulate a record where request.body exists but lacks a "data" dict,
    # so the normalization falls back to using "body" directly for messages/system/tools.
    rec = cast(
        "LogRecord",
        {
            "timing": {
                "start": _epoch("2026-06-05T12:00:00+00:00"),
                "completionStart": None,
                "end": _epoch("2026-06-05T12:00:01+00:00"),
            },
            "status": "success",
            "model": "m",
            "request": {
                "body": {
                    "messages": [{"role": "user", "content": "fallback body message"}],
                    "system": "fallback system",
                    "tools": [{"name": "FallbackTool"}],
                }
            },
            "response": {"choices": [{"message": {"content": "hi"}}]},
            "error": None,
        },
    )
    out = normalize_record(rec, {})
    assert out["messages"] == [{"role": "user", "content": "fallback body message"}]
    assert out["system"] == "fallback system"
    assert out["tools"] == [{"name": "FallbackTool"}]


def test_normalize_extracts_response_and_usage():
    out = normalize_record(_raw_record(), {})
    assert out["response"] == {"role": "assistant", "content": "hi"}
    assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 2}


def test_normalize_passes_through_timing():
    out = normalize_record(_raw_record(), {})
    assert out["timing"] == {
        "start": _epoch("2026-06-05T12:00:00+00:00"),
        "completionStart": None,
        "end": _epoch("2026-06-05T12:00:01+00:00"),
    }


def test_normalize_resolves_hashes():
    rec = _raw_record()
    rec["request"]["body"]["data"]["system"] = "hash:s"
    out = normalize_record(rec, {"hash:s": "resolved system"})
    assert out["system"] == "resolved system"


def test_normalize_tolerates_missing_pieces():
    out = normalize_record(cast("LogRecord", {"status": "failure", "error": "boom"}), {})
    assert out["messages"] == []
    assert out["tools"] == []
    assert out["response"] == {}
    assert out["usage"] == {}
    assert out["error"] == "boom"
    # A main-loop record (no proxy headers) carries no subagent id.
    assert out["agent_id"] is None


def test_normalize_extracts_subagent_agent_id():
    rec = _raw_record()
    rec["request"]["headers"] = {
        "x-claude-code-agent-id": "a27b7c3e5cb6db524",
    }
    out = normalize_record(rec, {})
    assert out["agent_id"] == "a27b7c3e5cb6db524"


def test_normalize_agent_id_none_without_header():
    # Main-loop requests have no x-claude-code-agent-id header.
    out = normalize_record(_raw_record(), {})
    assert out["agent_id"] is None


# --- _resolve_hashes ---


def test_resolve_hashes_replaces_known_hashes():
    obj = {"msg": "hash:abc123", "nested": ["hash:def456"]}
    strings = {"hash:abc123": "hello", "hash:def456": "world"}
    result = _resolve_hashes(obj, strings)
    assert result == {"msg": "hello", "nested": ["world"]}


def test_resolve_hashes_leaves_unknown_hashes():
    obj = {"msg": "hash:unknown"}
    strings = {"hash:abc123": "hello"}
    result = _resolve_hashes(obj, strings)
    assert result == {"msg": "hash:unknown"}


def test_resolve_hashes_leaves_primitives_unchanged():
    assert _resolve_hashes(None, {}) is None
    assert _resolve_hashes(42, {}) == 42
    assert _resolve_hashes(True, {}) is True  # noqa: FBT003
    assert _resolve_hashes("plain string", {}) == "plain string"


# --- normalize_record hash resolution ---


def test_normalize_record_resolves_hashes():
    """normalize_record resolves hash pointers in request and response."""
    strings = {"hash:abc123": "resolved content"}
    rec = {
        "timing": {"start": 1.0, "completionStart": None, "end": 2.0},
        "status": "success",
        "model": "m",
        "request": {
            "body": {
                "messages": [{"role": "user", "content": "hash:abc123"}],
                "system": "hash:abc123",
            },
        },
        "response": {
            "choices": [{"message": {"content": "hash:abc123"}}],
            "usage": {"prompt_tokens": 10},
        },
        "error": None,
    }
    out = normalize_record(rec, strings)
    assert out["messages"] == [{"role": "user", "content": "resolved content"}]
    assert out["system"] == "resolved content"
    assert out["response"]["content"] == "resolved content"


# --- extract_alias ---


def _naming_record(content: str) -> LogRecord:
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


def test_extract_alias_from_name_payload():
    assert extract_alias(_naming_record('{"name": "agent-logs-web-viewer"}')) == (
        "agent-logs-web-viewer"
    )


def test_extract_alias_ignores_title_payload():
    assert extract_alias(_naming_record('{"title": "Build a web viewer"}')) is None


def test_extract_alias_none_for_freeform_and_missing():
    assert extract_alias(_naming_record("hi there")) is None
    assert extract_alias(_naming_record('{"name": ""}')) is None
    assert extract_alias(cast("LogRecord", {})) is None


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


def test_read_session_normalizes_and_resolves(tmp_path: Path):
    project = tmp_path / "proj"
    sdir = _write_session(project, "litellm-bedrock", "s1", [_raw_record()])
    (sdir / "strings.jsonl").write_text(
        json.dumps({"hash": "hash:s", "original": "X"}) + "\n", encoding="utf-8"
    )
    data = read_session(project, "s1")
    reqs = data["reqs"]
    assert len(reqs) == 1
    assert reqs[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert data["session_meta"] is not None
    assert data["session_meta"]["count"] == 1
    assert data["session_meta"]["session_id"] == "s1"


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


def test_read_session_merges_across_providers(tmp_path: Path):
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
    data = read_session(project, "s1")
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


def test_list_projects_filters_to_those_with_logs(tmp_path: Path):
    tool_dir = tmp_path / "tool"
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
    projects = list_projects(tool_dir)
    assert [p["path"] for p in projects] == [str(with_logs)]
    assert projects[0]["sessions"] == 1
    assert projects[0]["id"] == 0


def test_list_projects_empty_without_registry(tmp_path: Path):
    assert list_projects(tmp_path / "nope") == []


# --- .agent_stats_leaf grouping --------------------------------------------


def test_list_projects_aggregates_marked_group(tmp_path: Path):
    """Two projects under a .agent_stats_leaf marker collapse to one entry."""
    tool_dir = tmp_path / "tool"
    (tool_dir / ".agent-launches").mkdir(parents=True)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".agent_stats_leaf").write_text("batch-feb\n", encoding="utf-8")

    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{a}\n{b}\n", encoding="utf-8")

    projects = list_projects(tool_dir)
    assert len(projects) == 1
    p = projects[0]
    assert p["name"] == "batch-feb"
    assert p["path"] == str(runs)
    assert p["sessions"] == 2
    assert p["last_ts"] == _epoch("2026-06-05T00:00:00+00:00")


def test_list_sessions_unions_group_members(tmp_path: Path):
    """list_sessions over a member-path list merges sessions from every member."""
    runs = tmp_path / "runs"
    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])

    sessions = list_sessions([a, b])
    assert [s["session_id"] for s in sessions] == ["s2", "s1"]


def test_unmarked_projects_stay_separate(tmp_path: Path):
    """Without a marker, each project remains its own entry (regression guard)."""
    tool_dir = tmp_path / "tool"
    (tool_dir / ".agent-launches").mkdir(parents=True)
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session(a, "litellm-bedrock", "s1", [_ts_rec("2026-06-01T00:00:00+00:00", model="m/a")])
    _write_session(b, "litellm-bedrock", "s2", [_ts_rec("2026-06-05T00:00:00+00:00", model="m/b")])
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{a}\n{b}\n", encoding="utf-8")

    projects = list_projects(tool_dir)
    assert {p["name"] for p in projects} == {"proj-a", "proj-b"}


# --- _read_last_record_ts ---


def test_read_last_record_ts_returns_last_ts(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        json.dumps(_ts_rec("2026-06-01T00:00:00+00:00"))
        + "\n"
        + json.dumps(_ts_rec("2026-06-05T12:00:00+00:00"))
        + "\n",
        encoding="utf-8",
    )
    assert _read_last_record_ts(f) == _epoch("2026-06-05T12:00:00+00:00")


def test_read_last_record_ts_returns_none_for_empty_file(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text("", encoding="utf-8")
    assert _read_last_record_ts(f) is None


def test_read_last_record_ts_returns_none_for_missing_file(tmp_path: Path):
    assert _read_last_record_ts(tmp_path / "nope.jsonl") is None


def test_read_last_record_ts_handles_single_record(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    assert _read_last_record_ts(f) == _epoch("2026-06-05T00:00:00+00:00")


def test_read_last_record_ts_handles_no_trailing_newline(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")),
        encoding="utf-8",
    )
    assert _read_last_record_ts(f) == _epoch("2026-06-05T00:00:00+00:00")


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
    assert _read_last_record_ts(f) == _epoch("2026-06-05T12:00:00+00:00")


def test_read_last_record_ts_handles_non_json_lines(tmp_path: Path):
    f = tmp_path / "messages.jsonl"
    f.write_text(
        "not json\n" + json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    assert _read_last_record_ts(f) == _epoch("2026-06-05T00:00:00+00:00")


# --- _lightweight_project_summary ---


def test_lightweight_project_summary_empty_project(tmp_path: Path):
    project = tmp_path / "proj"
    assert _lightweight_project_summary(project) == (0, None)


def test_lightweight_project_summary_single_session(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    count, last_ts = _lightweight_project_summary(project)
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
    count, last_ts = _lightweight_project_summary(project)
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
    count, last_ts = _lightweight_project_summary(project)
    assert count == 1
    assert last_ts == _epoch("2026-06-05T00:00:00+00:00")


def test_lightweight_project_summary_skips_empty_sessions(tmp_path: Path):
    """Session dir without a messages.jsonl should be skipped."""
    project = tmp_path / "proj"
    (project / ".claude" / "litellm-logs" / "litellm-bedrock" / "empty").mkdir(parents=True)
    count, last_ts = _lightweight_project_summary(project)
    assert count == 0
    assert last_ts is None


def test_list_projects_lightweight_produces_same_shape(tmp_path: Path):
    """Output dict must have the same keys as before the optimization."""
    tool_dir = tmp_path / "tool"
    (tool_dir / ".agent-launches").mkdir(parents=True)
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{project}\n", encoding="utf-8")
    projects = list_projects(tool_dir)
    assert len(projects) == 1
    p = projects[0]
    assert set(p.keys()) == {"id", "path", "name", "sessions", "last_ts"}
    assert p["sessions"] == 1
    assert p["last_ts"] == _epoch("2026-06-05T00:00:00+00:00")


def test_projects_fingerprint_reflects_changes(tmp_path: Path):
    """Fingerprint changes when a record is appended anywhere across projects."""
    tool_dir = tmp_path / "tool"
    (tool_dir / ".agent-launches").mkdir(parents=True)
    project = tmp_path / "proj"
    (tool_dir / ".agent-launches" / "projects.txt").write_text(f"{project}\n", encoding="utf-8")
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_ts_rec("2026-06-05T00:00:00+00:00", model="m/a")],
    )
    fp1 = projects_fingerprint(tool_dir)
    assert isinstance(fp1["mtime"], int)
    assert fp1["size"] > 0

    # Append a record in a second session — fingerprint should change.
    _write_session(
        project,
        "litellm-bedrock",
        "s2",
        [_ts_rec("2026-06-05T01:00:00+00:00", model="m/b")],
    )
    fp2 = projects_fingerprint(tool_dir)
    assert fp2["mtime"] != fp1["mtime"] or fp2["size"] != fp1["size"]


def test_projects_fingerprint_null_when_no_registry(tmp_path: Path):
    """No registry file → null fingerprint."""
    tool_dir = tmp_path / "nope"
    assert projects_fingerprint(tool_dir) == {"mtime": None, "size": None}


# --- arg parsing ---


def test_parse_port_default():
    assert _parse_port([]) == 8765


def test_parse_port_custom():
    assert _parse_port(["--port", "9000"]) == 9000


def test_parse_port_rejects_non_integer():
    assert _parse_port(["--port", "abc"]) is None


def test_parse_port_rejects_out_of_range():
    assert _parse_port(["--port", "0"]) is None
    assert _parse_port(["--port", "70000"]) is None


def test_parse_port_help_returns_none():
    assert _parse_port(["-h"]) is None


def test_parse_port_unknown_arg():
    assert _parse_port(["--bogus"]) is None


# --- resolve_static (path mapping + traversal safety) ---


def test_resolve_static_maps_root_to_index(tmp_path: Path):
    assert resolve_static(tmp_path, "/") == (tmp_path / "index.html").resolve()


def test_resolve_static_maps_named_asset(tmp_path: Path):
    assert resolve_static(tmp_path, "/app.js") == (tmp_path / "app.js").resolve()
    assert resolve_static(tmp_path, "/styles.css") == (tmp_path / "styles.css").resolve()


def test_resolve_static_rejects_traversal(tmp_path: Path):
    page = tmp_path / "logs_page"
    page.mkdir()
    # Escaping the page dir must be refused, not resolved to a sibling file.
    assert resolve_static(page, "/../logs.py") is None
    assert resolve_static(page, "/../../etc/passwd") is None


# --- meta.json caching (_read_meta_json / _write_meta_json / _scan_session_meta) ---


def _write_meta_file(session_dir: Path, meta: dict) -> Path:
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
    cached = _read_meta_json(sdir)
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
    assert _read_meta_json(sdir) is None


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
    import os
    import time

    past = time.time() - 60
    os.utime(sdir / "meta.json", (past, past))
    # Append another record to messages.jsonl to make it newer.
    msg_file = sdir / "messages.jsonl"
    with msg_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_ts_rec("2026-06-06T00:00:00+00:00", model="m/b")) + "\n")
    assert _read_meta_json(sdir) is None


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
    import time

    time.sleep(0.01)
    # Touch meta.json so it passes the staleness check but fails JSON parse.
    (sdir / "meta.json").write_text("still not json {{{", encoding="utf-8")
    assert _read_meta_json(sdir) is None


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
    assert _read_meta_json(sdir) is None


def test_scan_session_meta_uses_cache(tmp_path: Path):
    """When meta.json is fresh, _scan_session_meta returns cached data."""
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
    result = _scan_session_meta(sdir, "litellm-bedrock")
    assert result is not None
    assert result["count"] == 5
    assert result["last_ts"] == _epoch("2026-06-06T00:00:00+00:00")
    assert result["models"] == ["a", "b"]
    assert result["alias"] == "cached-alias"
    assert result["title"] == "Cached Title"
    assert result["provider"] == "litellm-bedrock"
    assert result["session_id"] == "s1"


def test_scan_session_meta_falls_back_without_cache(tmp_path: Path):
    """Without meta.json, _scan_session_meta scans messages.jsonl and seeds cache."""
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
    result = _scan_session_meta(sdir, "litellm-bedrock")
    assert result is not None
    assert result["count"] == 2
    assert result["models"] == ["a", "b"]
    # Should have seeded the cache for next time.
    cached = _read_meta_json(sdir)
    assert cached is not None
    assert cached["count"] == 2
    assert cached["models"] == ["a", "b"]


def test_write_and_read_meta_json_round_trip(tmp_path: Path):
    """_write_meta_json produces a file that _read_meta_json can consume."""
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "messages.jsonl").write_text(
        json.dumps(_ts_rec("2026-06-05T00:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    _write_meta_json(
        sdir,
        {
            "count": 3,
            "last_ts": _epoch("2026-06-05T02:00:00+00:00"),
            "models": ["x", "y"],
            "alias": "test-alias",
            "title": "Test Title",
        },
    )
    cached = _read_meta_json(sdir)
    assert cached is not None
    assert cached["count"] == 3
    assert cached["last_ts"] == _epoch("2026-06-05T02:00:00+00:00")
    assert cached["models"] == ["x", "y"]
    assert cached["alias"] == "test-alias"
    assert cached["title"] == "Test Title"


# --- background server: state file + liveness ------------------------------


def test_state_file_path(tmp_path: Path):
    assert _state_file(tmp_path) == tmp_path / ".agent-launches" / "logs-server.json"


def test_read_state_missing_returns_none(tmp_path: Path):
    assert _read_state(tmp_path) is None


def test_read_state_corrupt_returns_none(tmp_path: Path):
    (tmp_path / ".agent-launches").mkdir()
    _state_file(tmp_path).write_text("not json {{{", encoding="utf-8")
    assert _read_state(tmp_path) is None


def test_read_state_rejects_wrong_shape(tmp_path: Path):
    (tmp_path / ".agent-launches").mkdir()
    # Missing/wrong-typed pid and port must be rejected.
    _state_file(tmp_path).write_text(json.dumps({"pid": "x", "port": 8765}), encoding="utf-8")
    assert _read_state(tmp_path) is None


def test_write_then_read_state_round_trip(tmp_path: Path):
    _write_state(tmp_path, pid=4242, port=8765)
    state = _read_state(tmp_path)
    assert state == {"pid": 4242, "port": 8765}


def test_pid_alive_true_for_running(monkeypatch):
    monkeypatch.setattr(logs_mod.os, "kill", lambda pid, sig: None)
    assert _pid_alive(123) is True


def test_pid_alive_false_for_dead(monkeypatch):
    def _kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(logs_mod.os, "kill", _kill)
    assert _pid_alive(123) is False


def test_pid_alive_true_for_permission_error(monkeypatch):
    def _kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(logs_mod.os, "kill", _kill)
    assert _pid_alive(123) is True


def test_running_server_returns_state_when_alive(tmp_path: Path, monkeypatch):
    _write_state(tmp_path, pid=4242, port=9001)
    monkeypatch.setattr(logs_mod, "_pid_alive", lambda pid: True)
    state = _running_server(tmp_path)
    assert state == {"pid": 4242, "port": 9001}


def test_running_server_removes_stale_file_when_dead(tmp_path: Path, monkeypatch):
    _write_state(tmp_path, pid=4242, port=9001)
    monkeypatch.setattr(logs_mod, "_pid_alive", lambda pid: False)
    assert _running_server(tmp_path) is None
    assert not _state_file(tmp_path).exists()


def test_running_server_none_when_no_file(tmp_path: Path):
    assert _running_server(tmp_path) is None


# --- background server: run() dispatch -------------------------------------


def test_run_stop_dispatches_to_stop(tmp_path: Path, monkeypatch):
    called = {}

    def _fake_stop(td):
        called["stop"] = td
        return 0

    monkeypatch.setattr(logs_mod, "_stop", _fake_stop)
    assert run(["--stop"], tmp_path) == 0
    assert called["stop"] == tmp_path


def test_run_stop_rejects_extra_args(tmp_path: Path):
    assert run(["--stop", "--port", "9000"], tmp_path) == 1


def test_run_foreground_dispatches_to_serve_foreground(tmp_path: Path, monkeypatch):
    called = {}

    def _fake_fg(td, port):
        called["fg"] = (td, port)
        return 0

    monkeypatch.setattr(logs_mod, "_serve_foreground", _fake_fg)
    assert run(["--foreground", "--port", "9000"], tmp_path) == 0
    assert called["fg"] == (tmp_path, 9000)


def test_run_already_running_prints_connect_line_and_skips_spawn(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(logs_mod, "_running_server", lambda td: {"pid": 1, "port": 9123})
    spawned = {}

    def _fake_spawn(td, port):
        spawned["x"] = 1
        return 0

    monkeypatch.setattr(logs_mod, "_spawn_background", _fake_spawn)
    assert run(["--port", "8765"], tmp_path) == 0
    assert "x" not in spawned
    out = capsys.readouterr().out
    assert out.strip() == "LiteLLM log viewer running at http://127.0.0.1:9123"


def test_run_spawns_when_not_running(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(logs_mod, "_running_server", lambda td: None)
    called = {}

    def _fake_spawn(td, port):
        called["spawn"] = (td, port)
        return 0

    monkeypatch.setattr(logs_mod, "_spawn_background", _fake_spawn)
    assert run(["--port", "9000"], tmp_path) == 0
    assert called["spawn"] == (tmp_path, 9000)


def test_run_help_returns_zero(tmp_path: Path):
    assert run(["-h"], tmp_path) == 0


# --- background server: _stop ----------------------------------------------


def test_stop_when_not_running(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(logs_mod, "_running_server", lambda td: None)
    assert _stop(tmp_path) == 0
    assert "no viewer is running" in capsys.readouterr().out


def test_stop_sends_sigterm_and_removes_state(tmp_path: Path, monkeypatch, capsys):
    _write_state(tmp_path, pid=4242, port=9001)
    monkeypatch.setattr(logs_mod, "_running_server", lambda td: {"pid": 4242, "port": 9001})
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(logs_mod.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    # Report the PID as dead immediately so _stop doesn't spin on the wait loop.
    monkeypatch.setattr(logs_mod, "_pid_alive", lambda pid: False)
    assert _stop(tmp_path) == 0
    assert (4242, logs_mod.signal.SIGTERM) in signals
    assert not _state_file(tmp_path).exists()
    assert "viewer stopped" in capsys.readouterr().out


# --- background server: end-to-end (spawns real processes) -----------------


def test_background_server_start_and_stop(tmp_path: Path):
    """
    Full handshake: `agent logs` spawns a detached child that records a live
    PID/port, then `agent logs stop` terminates it and clears the state file.
    """
    import os
    import subprocess
    import sys
    import time

    # Re-run the real CLI against a temp tool_dir on an unusual base port to
    # avoid colliding with a developer's own running viewer.
    env = {**os.environ, "AGENT_LOGS_TOOL_DIR": str(tmp_path)}
    start = subprocess.run(
        [sys.executable, "-m", "agent_wrap", "logs", "--port", "8801"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    state_file = tmp_path / ".agent-launches" / "logs-server.json"
    try:
        assert start.returncode == 0, start.stderr
        assert "LiteLLM log viewer running at http://127.0.0.1:" in start.stdout
        assert state_file.is_file()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pid = state["pid"]
        # The child should be alive.
        assert _pid_alive(pid)
    finally:
        stop = subprocess.run(
            [sys.executable, "-m", "agent_wrap", "logs", "--stop"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    assert stop.returncode == 0, stop.stderr
    assert "viewer stopped" in stop.stdout
    assert not state_file.exists()

    # The child process should be gone (a zombie counts as dead — it may linger
    # unreaped briefly when its launching parent has already exited).
    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(pid), f"viewer pid {pid} still alive after stop"
