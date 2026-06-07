# This file has been created with the assistance of an AI tool.
"""Tests for the `logs` subcommand's data access and record normalization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_wrap.commands.logs import (
    _parse_port,
    extract_alias,
    list_projects,
    list_sessions,
    load_strings,
    normalize_record,
    read_session,
    resolve,
    resolve_static,
    session_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

# --- resolve (hash replacement + wrap-ref stripping) ---


def test_resolve_replaces_known_hashes():
    strings = {"hash:abc": "the original text"}
    assert resolve("hash:abc", strings) == "the original text"


def test_resolve_leaves_unknown_hashes_intact():
    # A missing strings.jsonl entry should remain visible, not blanked.
    assert resolve("hash:missing", {}) == "hash:missing"


def test_resolve_recurses_nested_structures():
    strings = {"hash:x": "X", "hash:y": "Y"}
    obj = {"a": "hash:x", "b": ["hash:y", "plain", {"c": "hash:x"}]}
    assert resolve(obj, strings) == {"a": "X", "b": ["Y", "plain", {"c": "X"}]}


def test_resolve_strips_wrap_ref_bookkeeping():
    # Pass 1 pops wrap-ref-id from dicts and lists, so it's not in the output.
    obj = {"k": "v", "wrap-ref-id": "3"}
    assert resolve(obj, {}) == {"k": "v"}
    lst = ["wrap-ref-id:0", "keep"]
    assert resolve(lst, {}) == ["keep"]


def test_resolve_reconstructs_wrap_ref_references():
    # Simulate the callback's output for a list containing two references to the same dict.
    canonical = {"content": "hello", "wrap-ref-id": "0"}
    obj_with_refs = [canonical, "wrap-ref:0"]
    resolved = resolve(obj_with_refs, {})
    # Both items should be dicts with identical content.
    assert resolved[0] == {"content": "hello"}
    assert resolved[1] == {"content": "hello"}
    # They MUST be the exact same object in memory to correctly preserve the reference graph.
    assert resolved[0] is resolved[1]
    # No wrap-ref strings should remain.
    assert "wrap-ref:0" not in str(resolved)
    assert "wrap-ref-id" not in str(resolved)


def test_resolve_resolves_hashes_inside_wrap_ref():
    # Ensure that hashes inside a canonical wrap-ref object are properly resolved.
    canonical = {"content": "hash:abc123", "wrap-ref-id": "0"}
    obj_with_refs = [canonical, "wrap-ref:0"]
    strings = {"hash:abc123": "the original text"}
    resolved = resolve(obj_with_refs, strings)
    assert resolved[0] == {"content": "the original text"}
    assert resolved[1] == {"content": "the original text"}


def test_resolve_handles_circular_references():
    # Simulate a canonical object that references itself.
    # The callback would serialize this as a dict with wrap-ref-id, and a child pointing to it.
    canonical = {"name": "self", "child": "wrap-ref:0", "wrap-ref-id": "0"}
    resolved = resolve([canonical], {})
    # The resolved object should have a child that points to itself.
    assert resolved[0]["name"] == "self"
    assert resolved[0]["child"] is resolved[0]
    # No wrap-ref strings should remain.
    assert "wrap-ref:0" not in str(resolved)


# --- normalize_record ---


def _raw_record() -> dict:
    return {
        "ts": "2026-06-05T12:00:00+00:00",
        "status": "success",
        "model": "us.anthropic.claude-opus-4-8",
        "request": {
            # Top-level messages is a LiteLLM placeholder and must be ignored.
            "messages": [{"role": "user", "content": "default-message-value"}],
            "proxy_server_request": {
                "body": {
                    "data": {
                        "messages": [{"role": "user", "content": "hello"}],
                        "system": "be brief",
                        "tools": [{"name": "Read"}],
                    }
                }
            },
        },
        "response": {
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    }


def test_normalize_pulls_real_request_data():
    out = normalize_record(_raw_record(), {})
    assert out["messages"] == [{"role": "user", "content": "hello"}]
    assert out["system"] == "be brief"
    assert out["tools"] == [{"name": "Read"}]


def test_normalize_falls_back_to_body_when_data_is_not_dict():
    # Simulate a record where proxy_server_request.body exists but lacks a "data" dict,
    # so the normalization falls back to using "body" directly for messages/system/tools.
    rec = {
        "ts": "2026-06-05T12:00:00+00:00",
        "status": "success",
        "model": "m",
        "request": {
            "proxy_server_request": {
                "body": {
                    "messages": [{"role": "user", "content": "fallback body message"}],
                    "system": "fallback system",
                    "tools": [{"name": "FallbackTool"}],
                }
            },
        },
        "response": {"choices": [{"message": {"content": "hi"}}]},
    }
    out = normalize_record(rec, {})
    assert out["messages"] == [{"role": "user", "content": "fallback body message"}]
    assert out["system"] == "fallback system"
    assert out["tools"] == [{"name": "FallbackTool"}]


def test_normalize_ignores_placeholder_messages():
    out = normalize_record(_raw_record(), {})
    # The "default-message-value" placeholder must never surface.
    assert all(m["content"] != "default-message-value" for m in out["messages"])


def test_normalize_extracts_response_and_usage():
    out = normalize_record(_raw_record(), {})
    assert out["response"] == {"role": "assistant", "content": "hi"}
    assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 2}


def test_normalize_resolves_hashes():
    rec = _raw_record()
    rec["request"]["proxy_server_request"]["body"]["data"]["system"] = "hash:s"
    out = normalize_record(rec, {"hash:s": "resolved system"})
    assert out["system"] == "resolved system"


def test_normalize_resolves_wrap_ref_from_discarded_fields():
    # Simulate a record where the canonical object is in the top-level
    # request.messages (a LiteLLM placeholder that is discarded),
    # but proxy_server_request.body.data.messages references it.
    rec = {
        "ts": "2026-06-05T12:00:00+00:00",
        "status": "success",
        "model": "m",
        "request": {
            "messages": [{"wrap-ref-id": "0", "content": "canonical"}],
            "proxy_server_request": {"body": {"data": {"messages": ["wrap-ref:0"]}}},
        },
        "response": {"choices": [{"message": {"content": "hi"}}]},
    }
    out = normalize_record(rec, {})
    # The discarded field's canonical object should still be resolved
    assert out["messages"] == [{"content": "canonical"}]
    # It should be a deep copy, not the same object in memory
    assert out["messages"][0] is not rec["request"]["messages"][0]


def test_normalize_tolerates_missing_pieces():
    out = normalize_record({"ts": "t", "status": "failure", "error": "boom"}, {})
    assert out["messages"] == []
    assert out["tools"] == []
    assert out["response"] == {}
    assert out["usage"] == {}
    assert out["error"] == "boom"
    # A main-loop record (no proxy headers) carries no subagent id.
    assert out["agent_id"] is None


def test_normalize_extracts_subagent_agent_id():
    rec = _raw_record()
    rec["request"]["proxy_server_request"]["headers"] = {
        "x-claude-code-agent-id": "a27b7c3e5cb6db524",
    }
    out = normalize_record(rec, {})
    assert out["agent_id"] == "a27b7c3e5cb6db524"


def test_normalize_agent_id_none_without_header():
    # Main-loop requests have no x-claude-code-agent-id header.
    out = normalize_record(_raw_record(), {})
    assert out["agent_id"] is None


# --- extract_alias ---


def _naming_record(content: str) -> dict:
    return {"response": {"choices": [{"message": {"role": "assistant", "content": content}}]}}


def test_extract_alias_from_name_payload():
    assert extract_alias(_naming_record('{"name": "agent-logs-web-viewer"}')) == (
        "agent-logs-web-viewer"
    )


def test_extract_alias_ignores_title_payload():
    assert extract_alias(_naming_record('{"title": "Build a web viewer"}')) is None


def test_extract_alias_none_for_freeform_and_missing():
    assert extract_alias(_naming_record("hi there")) is None
    assert extract_alias(_naming_record('{"name": ""}')) is None
    assert extract_alias({}) is None


# --- filesystem helpers ---


def _write_session(project: Path, provider: str, session_id: str, records: list[dict]) -> Path:
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
        [{"ts": "2026-06-01T00:00:00+00:00", "model": "m/a"}],
    )
    _write_session(
        project,
        "litellm-bedrock",
        "sess-new",
        [
            {"ts": "2026-06-05T00:00:00+00:00", "model": "m/b"},
            {"ts": "2026-06-05T01:00:00+00:00", "model": "m/b"},
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
            {"ts": "2026-06-05T00:00:00+00:00", "model": "m/a"},
            _naming_record('{"name": "derived-slug"}') | {"ts": "2026-06-05T00:01:00+00:00"},
        ],
    )
    assert list_sessions(project)[0]["alias"] == "derived-slug"


def test_list_sessions_alias_file_wins_over_derivation(tmp_path: Path):
    project = tmp_path / "proj"
    sdir = _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [_naming_record('{"name": "derived-slug"}') | {"ts": "2026-06-05T00:00:00+00:00"}],
    )
    (sdir / "alias").write_text("file-slug\n", encoding="utf-8")
    assert list_sessions(project)[0]["alias"] == "file-slug"


def test_list_sessions_alias_none_when_absent(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [{"ts": "2026-06-05T00:00:00+00:00", "model": "m/a"}],
    )
    assert list_sessions(project)[0]["alias"] is None


def test_read_session_normalizes_and_resolves(tmp_path: Path):
    project = tmp_path / "proj"
    sdir = _write_session(project, "litellm-bedrock", "s1", [_raw_record()])
    (sdir / "strings.jsonl").write_text(
        json.dumps({"hash": "hash:s", "original": "X"}) + "\n", encoding="utf-8"
    )
    reqs = read_session(project, "s1")
    assert len(reqs) == 1
    assert reqs[0]["messages"] == [{"role": "user", "content": "hello"}]


def test_session_fingerprint_reflects_file(tmp_path: Path):
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [{"ts": "2026-06-05T00:00:00+00:00", "model": "m/a"}],
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
        [{"ts": "2026-06-01T00:00:00+00:00", "model": "m/a"}],
    )
    _write_session(
        project,
        "litellm-deepseek",
        "s1",
        [{"ts": "2026-06-05T00:00:00+00:00", "model": "m/b"}],
    )
    sessions = list_sessions(project)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "s1"
    assert s["providers"] == ["litellm-bedrock", "litellm-deepseek"]
    assert s["count"] == 2
    assert s["models"] == ["a", "b"]
    assert s["first_ts"] == "2026-06-01T00:00:00+00:00"
    assert s["last_ts"] == "2026-06-05T00:00:00+00:00"


def test_list_sessions_providers_field_shape(tmp_path: Path):
    """Single-provider sessions still have a providers list (of length 1)."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [{"ts": "2026-06-05T00:00:00+00:00", "model": "m/a"}],
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
                "ts": "2026-06-01T00:00:00+00:00",
                "status": "success",
                "model": "m/a",
                "request": {
                    "proxy_server_request": {
                        "body": {
                            "data": {"messages": [{"role": "user", "content": "from bedrock"}]}
                        }
                    }
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
                "ts": "2026-06-01T00:01:00+00:00",
                "status": "success",
                "model": "m/b",
                "request": {
                    "proxy_server_request": {
                        "body": {
                            "data": {"messages": [{"role": "user", "content": "from deepseek"}]}
                        }
                    }
                },
                "response": {"choices": [{"message": {"content": "deepseek reply"}}]},
            },
        ],
    )
    reqs = read_session(project, "s1")
    assert len(reqs) == 2
    assert reqs[0]["messages"] == [{"role": "user", "content": "from bedrock"}]
    assert reqs[1]["messages"] == [{"role": "user", "content": "from deepseek"}]


def test_session_fingerprint_combines_across_providers(tmp_path: Path):
    """Fingerprint reflects max mtime and combined size across providers."""
    project = tmp_path / "proj"
    _write_session(
        project,
        "litellm-bedrock",
        "s1",
        [{"ts": "2026-06-05T00:00:00+00:00", "model": "m/a"}],
    )
    _write_session(
        project,
        "litellm-deepseek",
        "s1",
        [
            {"ts": "2026-06-05T00:00:00+00:00", "model": "m/b"},
            {"ts": "2026-06-05T01:00:00+00:00", "model": "m/b"},
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
        [{"ts": "2026-06-05T00:00:00+00:00", "model": "m/a"}],
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
