# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for the `stats` subcommand — rendering and arg parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_wrap.cli.stats.display import render, render_source_breakdown
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.stats.usage_args import parse_usage_args


def _source_bucket(msgs: int, *, in_: int = 0) -> Bucket:
    b = Bucket()
    for _ in range(msgs):
        b.add(
            {
                "input_tokens": in_,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation": {},
            },
            0.0,
        )
    return b


# ---------------------------------------------------------------------------
# render — orphaned row
# ---------------------------------------------------------------------------


def test_render_includes_orphaned_row() -> None:
    """render() shows an <orphaned> row (accented in color, no text marker)."""
    b = Bucket()
    b.add(
        {
            "input_tokens": 1000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        0.0,
    )
    b.add(
        {
            "input_tokens": 1000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        0.0,
    )
    last_ts = datetime(2026, 6, 29, tzinfo=timezone.utc)
    orphaned = {"sessions": 1, "last_ts": last_ts, "total": b}
    out = render([], {}, None, None, orphaned=orphaned)
    assert "<orphaned>" in out
    assert "<orphaned> *" not in out


def test_render_without_orphaned_has_no_row() -> None:
    """When orphaned is None, no <orphaned> row appears."""
    out = render([], {}, None, None, orphaned=None)
    assert "<orphaned>" not in out


# ---------------------------------------------------------------------------
# Render source breakdown
# ---------------------------------------------------------------------------


def test_render_source_breakdown_lists_active_sources() -> None:
    by_source = {
        "native": {"bedrock/claude-opus-4-8": _source_bucket(3, in_=1000)},
        "standard_logging_object": {"bedrock/claude-opus-4-8": _source_bucket(2, in_=500)},
        "unrecoverable": {"bedrock/claude-opus-4-8": _source_bucket(1)},
    }
    out = render_source_breakdown(by_source, None, None)
    assert "Usage source breakdown (all time):" in out
    assert "native" in out
    assert "standard_logging_object" in out
    assert "unrecoverable" in out
    assert "TOTAL" in out


def test_render_source_breakdown_omits_zero_msg_sources() -> None:
    by_source = {"native": {"bedrock/claude-opus-4-8": _source_bucket(2, in_=100)}}
    out = render_source_breakdown(by_source, None, None)
    assert "native" in out
    assert "standard_logging_object" not in out


def test_render_source_breakdown_empty_when_no_activity() -> None:
    assert render_source_breakdown({}, None, None) == ""


def test_render_source_breakdown_merges_across_models() -> None:
    by_source = {
        "unrecoverable": {"bedrock/claude-opus-4-8": _source_bucket(1)},
        "native": {"bedrock/claude-haiku-4-5": _source_bucket(1, in_=1)},
    }
    out = render_source_breakdown(by_source, "2026-06-01", "2026-06-29")
    assert "Usage source breakdown (2026-06-01 … 2026-06-29):" in out
    assert "unrecoverable" in out
    assert "native" in out


# ---------------------------------------------------------------------------
# parse_usage_args
# ---------------------------------------------------------------------------


def test_parse_usage_args_verbose_short_flag(tmp_path: Path) -> None:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg), "-v"], usage_line="u", usage_text="u")
    assert parsed is not None
    assert parsed.verbose is True


def test_parse_usage_args_verbose_long_flag(tmp_path: Path) -> None:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg), "--verbose"], usage_line="u", usage_text="u")
    assert parsed is not None
    assert parsed.verbose is True


def test_parse_usage_args_verbose_defaults_false(tmp_path: Path) -> None:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg)], usage_line="u", usage_text="u")
    assert parsed is not None
    assert parsed.verbose is False
