# This file has been edited with the assistance of an AI tool.
"""Tests for the `usage` subcommand (agent_wrap.commands.usage)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_wrap.commands.usage import (
    Bucket,
    _parse_usage_args,
    _process_record,
    build_project_tree,
    cost_for,
    flatten_tree,
    fmt_cost,
    fmt_count,
    load_prices,
    load_projects,
    normalize_model,
    render,
    run,
    scan_project,
)

# --- normalize_model ---


def test_canonical_form():
    assert normalize_model("claude-opus-4-7") == "claude-opus-4-7"


def test_date_stamped_snapshot():
    assert normalize_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"


def test_bedrock_arn_form():
    assert normalize_model("anthropic.claude-opus-4-7-v1:0") == "claude-opus-4-7"


def test_display_name():
    assert normalize_model("Claude Opus 4.7") == "claude-opus-4-7"


def test_display_name_sonnet():
    assert normalize_model("Claude Sonnet 4.5") == "claude-sonnet-4-5"


def test_display_name_haiku():
    assert normalize_model("Claude Haiku 3.5") == "claude-haiku-3-5"


def test_empty_string():
    assert normalize_model("") is None


def test_non_claude_model():
    assert normalize_model("gpt-4o") is None


def test_version_first_form():
    assert normalize_model("claude-4-7-opus") == "claude-opus-4-7"


def test_date_suffix_only_when_at_end():
    assert normalize_model("anthropic.claude-sonnet-4-5-v1:0") == "claude-sonnet-4-5"
    assert (
        normalize_model("anthropic.claude-sonnet-4-5-20250514-v1:0") == "claude-sonnet-4-5-20250514"
    )


# --- cost_for ---


@pytest.fixture
def prices():
    return {
        "claude-sonnet-4-5": {
            "in": 3.0,
            "out": 15.0,
            "cw_5m": 3.75,
            "cw_1h": 4.0,
            "cr": 0.30,
        },
    }


def test_zero_usage(prices):
    usage = {"in": 0, "out": 0, "cw_5m": 0, "cw_1h": 0, "cr": 0}
    assert cost_for("claude-sonnet-4-5", usage, prices) == 0.0


def test_unknown_model():
    usage = {"in": 1000, "out": 500, "cw_5m": 0, "cw_1h": 0, "cr": 0}
    assert cost_for("unknown-model", usage, {}) is None


def test_known_model(prices):
    usage = {"in": 1_000_000, "out": 500_000, "cw_5m": 0, "cw_1h": 0, "cr": 0}
    c = cost_for("claude-sonnet-4-5", usage, prices)
    assert c is not None
    assert abs(c - 10.5) < 0.01  # 3.0 + 7.5 = 10.5


def test_empty_prices():
    usage = {"in": 1000, "out": 500, "cw_5m": 0, "cw_1h": 0, "cr": 0}
    assert cost_for("claude-sonnet-4-5", usage, {}) is None


def test_zero_usage_empty_prices():
    usage = {"in": 0, "out": 0, "cw_5m": 0, "cw_1h": 0, "cr": 0}
    assert cost_for("claude-sonnet-4-5", usage, {}) == 0.0


# --- _parse_usage_args ---


def test_minimal_valid_args(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/some/project\n")
    cache = tmp_path / "pricing.json"
    result = _parse_usage_args(["--cache", str(cache), str(reg)])
    assert result is not None
    assert result.cache_path == cache
    assert result.registry_path == reg
    assert result.region_label == "US East (N. Virginia)"
    assert result.refresh is False
    assert result.days_window == 30


def test_with_all_flags(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/some/project\n")
    cache = tmp_path / "pricing.json"
    result = _parse_usage_args(
        [
            "--cache",
            str(cache),
            str(reg),
            "--days",
            "7",
            "--region",
            "EU West (London)",
            "--refresh",
        ]
    )
    assert result is not None
    assert result.days_window == 7
    assert result.region_label == "EU West (London)"
    assert result.refresh is True


def test_missing_registry_path(tmp_path):
    cache = tmp_path / "cache.json"
    result = _parse_usage_args(["--cache", str(cache)])
    assert result is None


def test_registry_file_not_exists(tmp_path):
    cache = tmp_path / "cache.json"
    result = _parse_usage_args(["--cache", str(cache), "/nonexistent/projects.txt"])
    assert result is None


def test_help_flag():
    result = _parse_usage_args(["-h"])
    assert result is None


def test_long_help_flag():
    result = _parse_usage_args(["--help"])
    assert result is None


def test_days_zero(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/a\n")
    cache = tmp_path / "pricing.json"
    result = _parse_usage_args(
        [
            "--cache",
            str(cache),
            str(reg),
            "--days",
            "0",
        ]
    )
    assert result is not None
    assert result.days_window == 0


def test_days_negative_returns_none(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/a\n")
    cache = tmp_path / "pricing.json"
    result = _parse_usage_args(
        [
            "--cache",
            str(cache),
            str(reg),
            "--days",
            "-5",
        ]
    )
    assert result is None


def test_days_non_integer_returns_none(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/a\n")
    cache = tmp_path / "pricing.json"
    result = _parse_usage_args(
        [
            "--cache",
            str(cache),
            str(reg),
            "--days",
            "abc",
        ]
    )
    assert result is None


# --- load_projects ---


def test_load_projects_basic(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/a\n/b\n/c\n")
    result = load_projects(reg)
    assert result == [Path("/a"), Path("/b"), Path("/c")]


def test_load_projects_deduplicates(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/a\n/b\n/a\n/c\n/b\n")
    result = load_projects(reg)
    assert len(result) == 3
    assert result == [Path("/a"), Path("/b"), Path("/c")]


def test_load_projects_skips_empty_lines(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/a\n\n/b\n\n")
    result = load_projects(reg)
    assert len(result) == 2


def test_load_projects_strips_whitespace(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("  /a  \n  /b  \n")
    result = load_projects(reg)
    assert result == [Path("/a"), Path("/b")]


def test_load_projects_empty_file(tmp_path):
    reg = tmp_path / "projects.txt"
    reg.write_text("")
    result = load_projects(reg)
    assert result == []


# --- run ---


def test_run_empty_project_registry(tmp_path):
    launches = tmp_path / ".agent-launches"
    launches.mkdir()
    projects_file = launches / "projects.txt"
    projects_file.write_text("")
    cache = launches / "pricing.json"
    cache.touch()

    with patch("agent_wrap.commands.usage.load_projects", return_value=[]):
        rc = run([], tmp_path)
        assert rc == 0


def test_run_unknown_flag_treated_as_positional(tmp_path, capsys):
    launches = tmp_path / ".agent-launches"
    launches.mkdir()
    projects_file = launches / "projects.txt"
    projects_file.write_text("")

    rc = run(["--bogus-flag"], tmp_path)
    assert rc == 0


def test_run_help_returns_zero(tmp_path):
    rc = run(["--help"], tmp_path)
    assert rc == 0


# --- Bucket ---


def test_bucket_add_counts_tokens():
    b = Bucket()
    b.add(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 20,
        }
    )
    assert b.in_ == 100
    assert b.out == 50
    assert b.cw_5m == 30
    assert b.cw_1h == 0
    assert b.cr == 20
    assert b.msgs == 1


def test_bucket_merge():
    a = Bucket()
    a.in_ = 100
    a.out = 50
    a.msgs = 2

    b = Bucket()
    b.in_ = 30
    b.out = 20
    b.msgs = 1

    a.merge(b)
    assert a.in_ == 130
    assert a.out == 70
    assert a.msgs == 3


def test_bucket_usage_dict():
    b = Bucket()
    b.in_ = 10
    b.out = 20
    b.cw_5m = 5
    b.cw_1h = 3
    b.cr = 7
    assert b.usage_dict() == {
        "in": 10,
        "out": 20,
        "cw_5m": 5,
        "cw_1h": 3,
        "cr": 7,
    }


# --- build_project_tree ---


def test_single_project():
    rows = [
        {
            "path": "/home/user/project-a",
            "exists": True,
            "sessions": 5,
            "last_ts": None,
            "total": Bucket(),
            "cost": 1.5,
        }
    ]
    root = build_project_tree(rows)
    display = flatten_tree(root)
    assert len(display) == 1
    assert display[0].label.endswith("project-a")


def test_multiple_projects():
    rows = [
        {
            "path": "/home/user/project-a",
            "exists": True,
            "sessions": 5,
            "last_ts": None,
            "total": Bucket(),
            "cost": 1.5,
        },
        {
            "path": "/home/user/project-b",
            "exists": True,
            "sessions": 3,
            "last_ts": None,
            "total": Bucket(),
            "cost": 0.5,
        },
    ]
    root = build_project_tree(rows)
    display = flatten_tree(root)
    # Two projects sharing /home/user/ prefix → structural parent + 2 project rows = 3
    assert len(display) == 3
    structural = [d for d in display if d.is_structural]
    assert len(structural) == 1  # the shared /home/user/ parent
    projects = [d for d in display if not d.is_structural]
    assert len(projects) == 2


def test_missing_project():
    rows = [
        {
            "path": "/home/user/gone",
            "exists": False,
            "sessions": 0,
            "last_ts": None,
            "total": Bucket(),
            "cost": 0.0,
        }
    ]
    root = build_project_tree(rows)
    display = flatten_tree(root)
    assert "(missing)" in display[0].label


# --- fmt_count / fmt_cost ---


def test_fmt_count_under_thousand():
    assert fmt_count(999) == "999"


def test_fmt_count_thousand():
    assert fmt_count(1_000) == "1.0K"


def test_fmt_count_million():
    assert fmt_count(1_000_000) == "1.00M"


def test_fmt_count_billion():
    assert fmt_count(1_000_000_000) == "1.00G"


def test_fmt_count_mid_range():
    assert fmt_count(5_432) == "5.4K"


def test_fmt_cost_none():
    assert fmt_cost(None) == "?"


def test_fmt_cost_zero():
    assert fmt_cost(0.0) == "$0.00"


def test_fmt_cost_positive():
    assert fmt_cost(10.5) == "$10.50"


# --- normalize_model edge cases ---


def test_normalize_model_no_tier():
    assert normalize_model("claude-") is None


def test_normalize_model_no_version():
    assert normalize_model("claude-opus") is None


# --- _process_record ---


def test_process_record_non_assistant():
    buckets: dict = {}
    seen: set = set()
    rec = {
        "type": "user",
        "message": {"usage": {"input_tokens": 100}},
        "timestamp": "2025-01-01T00:00:00Z",
    }
    ts = _process_record(rec, seen, buckets)
    assert ts is not None
    assert len(buckets) == 0  # no bucket added for non-assistant


def test_process_record_empty_usage():
    buckets: dict = {}
    seen: set = set()
    rec = {"type": "assistant", "message": {"usage": None}, "timestamp": "2025-01-01T00:00:00Z"}
    ts = _process_record(rec, seen, buckets)
    assert ts is not None
    assert len(buckets) == 0


def test_process_record_duplicate_msg_id():
    from collections import defaultdict

    buckets: dict = defaultdict(lambda: defaultdict(Bucket))
    seen: set = set()
    rec = {
        "type": "assistant",
        "message": {
            "id": "msg_123",
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
        "timestamp": "2025-01-01T00:00:00Z",
    }
    _process_record(rec, seen, buckets)
    assert "msg_123" in seen
    assert buckets["2025-01-01"]["claude-sonnet-4-5"].in_ == 100
    # Duplicate should be skipped
    rec2 = dict(rec)
    _process_record(rec2, seen, buckets)
    assert buckets["2025-01-01"]["claude-sonnet-4-5"].in_ == 100  # unchanged


# --- scan_project ---


def test_scan_project_basic(tmp_path: Path):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session1.jsonl").write_text(
        '{"type":"assistant","message":{"id":"m1","model":"claude-sonnet-4-5","usage":{"input_tokens":100,"output_tokens":50}},"timestamp":"2025-01-15T10:00:00Z"}\n'
    )
    sessions, last_ts, buckets, exists = scan_project(tmp_path, set())
    assert sessions == 1
    assert exists is True
    assert last_ts is not None
    assert "2025-01-15" in buckets
    assert buckets["2025-01-15"]["claude-sonnet-4-5"].in_ == 100


def test_scan_project_dedup_across_files(tmp_path: Path):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    # Same message.id in two files (simulates resume/fork)
    line = '{"type":"assistant","message":{"id":"m_dup","model":"claude-sonnet-4-5","usage":{"input_tokens":200,"output_tokens":100}},"timestamp":"2025-01-15T10:00:00Z"}\n'
    (sessions / "s1.jsonl").write_text(line)
    (sessions / "s2.jsonl").write_text(line)
    seen: set = set()
    _s1, _, b1, _ = scan_project(tmp_path, seen)
    assert "m_dup" in seen
    assert b1["2025-01-15"]["claude-sonnet-4-5"].msgs == 1  # only counted once


def test_scan_project_malformed_jsonl(tmp_path: Path):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "bad.jsonl").write_text("not json\n{also bad\n")
    sessions, _last_ts, _buckets, exists = scan_project(tmp_path, set())
    assert sessions == 1
    assert exists is True
    assert len(_buckets) == 0  # no valid records


def test_scan_project_unreadable_file(tmp_path: Path, mocker):
    """Unreadable JSONL file (OSError during open) is gracefully skipped."""
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    good_file = sessions / "good.jsonl"
    good_file.write_text(
        '{"type":"assistant","message":{"id":"m1","model":"claude-sonnet-4-5","usage":{"input_tokens":50}},"timestamp":"2025-01-15T10:00:00Z"}\n'
    )
    bad_file = sessions / "bad.jsonl"
    bad_file.touch()
    # Make file unreadable
    bad_file.chmod(0o000)
    sessions_count, _last_ts, _buckets, exists = scan_project(tmp_path, set())
    # Should still count the good file
    assert sessions_count == 2  # both files exist as session files
    assert exists is True
    # Restore permissions so cleanup works
    bad_file.chmod(0o644)


def test_scan_project_missing_sessions_dir(tmp_path: Path):
    sessions, last_ts, buckets, exists = scan_project(tmp_path, set())
    assert sessions == 0
    assert last_ts is None
    assert buckets == {}
    assert exists is False


# --- load_prices ---


def test_load_prices_cache_hit(tmp_path: Path, mocker):
    import time

    cache = tmp_path / "pricing.json"
    cache.write_text(
        '{"region":"US East (N. Virginia)","fetched_at":'
        + str(int(time.time()))
        + ',"prices":{"claude-sonnet-4-5":{"in":1.0}}}'
    )
    mock_get = mocker.patch("agent_wrap.commands.usage._http_get")
    result = load_prices(cache)
    mock_get.assert_not_called()
    assert "claude-sonnet-4-5" in result


def test_load_prices_stale_cache_triggers_refetch(tmp_path: Path, mocker):
    cache = tmp_path / "pricing.json"
    cache.write_text(
        '{"region":"US East (N. Virginia)","fetched_at":0,"prices":{"claude-sonnet-4-5":{"in":1.0}}}'
    )
    mocker.patch("agent_wrap.commands.usage._http_get", side_effect=OSError("network"))
    result = load_prices(cache)
    # Falls back to stale cache
    assert "claude-sonnet-4-5" in result


def test_load_prices_refresh_bypasses_fresh_cache(tmp_path: Path, mocker):
    import time

    cache = tmp_path / "pricing.json"
    cache.write_text(
        '{"region":"US East (N. Virginia)","fetched_at":'
        + str(int(time.time()))
        + ',"prices":{"claude-sonnet-4-5":{"in":1.0}}}'
    )
    mock_get = mocker.patch("agent_wrap.commands.usage._http_get", side_effect=OSError("network"))
    result = load_prices(cache, refresh=True)
    mock_get.assert_called()
    # No fresh data returned, falls back to stale
    assert "claude-sonnet-4-5" in result


def test_load_prices_no_cache_no_network(tmp_path: Path, mocker, capsys):
    mocker.patch("agent_wrap.commands.usage._http_get", side_effect=OSError("offline"))
    result = load_prices(None)
    assert result == {}


# --- render ---


def test_render_empty_rows(prices):
    output = render([], {}, {}, prices, 30)
    assert "Total:" in output


def test_render_single_project(prices):
    b = Bucket()
    b.in_ = 100
    rows = [
        {
            "path": "/home/user/proj",
            "exists": True,
            "sessions": 1,
            "last_ts": None,
            "total": b,
            "cost": 0.5,
        }
    ]
    output = render(rows, {}, {}, prices, 30)
    assert "proj" in output


def test_render_unknown_model_cost():
    output = render(
        [
            {
                "path": "/home/user/proj",
                "exists": True,
                "sessions": 1,
                "last_ts": None,
                "total": Bucket(),
                "cost": None,  # unknown model
            }
        ],
        {},
        {},
        {},
        30,
    )
    assert "?" in output


def test_render_table_borders(prices):
    output = render(
        [
            {
                "path": "/home/user/proj",
                "exists": True,
                "sessions": 1,
                "last_ts": None,
                "total": Bucket(),
                "cost": 0.0,
            }
        ],
        {},
        {},
        prices,
        30,
    )
    # Table should have box-drawing borders
    assert "┌" in output
    assert "┐" in output
    assert "└" in output
    assert "┘" in output


# --- Tree logic edge cases ---


def test_tree_shared_prefix_renders_under_parent():
    rows = [
        {
            "path": "/home/user/proj-a",
            "exists": True,
            "sessions": 5,
            "last_ts": None,
            "total": Bucket(),
            "cost": 1.0,
        },
        {
            "path": "/home/user/proj-b",
            "exists": True,
            "sessions": 3,
            "last_ts": None,
            "total": Bucket(),
            "cost": 0.5,
        },
    ]
    root = build_project_tree(rows)
    display = flatten_tree(root)
    # proj-a and proj-b share /home/user/ prefix → structural parent + 2 project rows
    assert len(display) == 3
    structural = [d for d in display if d.is_structural]
    assert len(structural) == 1  # the shared /home/user/ parent


def test_tree_split_self_rows():
    """Project with a nested project path gets a '.' self-row."""
    rows = [
        {
            "path": "/home/user/proj",
            "exists": True,
            "sessions": 5,
            "last_ts": None,
            "total": Bucket(),
            "cost": 1.0,
        },
        {
            "path": "/home/user/proj/sub",
            "exists": True,
            "sessions": 2,
            "last_ts": None,
            "total": Bucket(),
            "cost": 0.5,
        },
    ]
    root = build_project_tree(rows)
    display = flatten_tree(root)
    dot_rows = [d for d in display if "." in d.label and not d.is_structural]
    assert len(dot_rows) == 1  # the proj's own row is split into '.'


def test_compress_multi_child_not_compressed():
    """Structural nodes with multiple children should not be compressed."""
    rows = [
        {
            "path": "/a/x",
            "exists": True,
            "sessions": 1,
            "last_ts": None,
            "total": Bucket(),
            "cost": 0.1,
        },
        {
            "path": "/a/y",
            "exists": True,
            "sessions": 1,
            "last_ts": None,
            "total": Bucket(),
            "cost": 0.2,
        },
    ]
    root = build_project_tree(rows)
    display = flatten_tree(root)
    # /a/ has two children, should not compress into a/x or /a/y
    structural = [d for d in display if d.is_structural]
    assert len(structural) == 1  # /a/ stays as structural
