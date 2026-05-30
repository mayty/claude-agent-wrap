# This file has been edited with the assistance of an AI tool.
"""Tests for the `usage` subcommand (agent_wrap.commands.usage)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_wrap.commands.usage import (
    Bucket,
    _parse_usage_args,
    build_project_tree,
    cost_for,
    flatten_tree,
    load_projects,
    normalize_model,
    run,
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
    assert len(display) >= 2


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


# --- import smoke tests ---


def test_run_exists():
    from agent_wrap.commands.usage import run

    assert callable(run)


def test_no_sys_modules_leak():
    import sys

    assert "agent_usage" not in sys.modules
