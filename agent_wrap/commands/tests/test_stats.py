# This file has been created with the assistance of an AI tool.
"""Tests for the `stats` subcommand's model→pricing matching."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING

from agent_wrap.commands.stats import (
    PriceSource,
    _aggregate_projects,
    _best_prefix_key,
    _collect_orphaned,
    _cost_record,
    render,
)
from agent_wrap.lib.buckets import Bucket
from agent_wrap.lib.tree import build_project_tree, flatten_tree

if TYPE_CHECKING:
    from pathlib import Path

# --- _best_prefix_key ---


def test_exact_key_beats_date_stamped_siblings():
    # Shortest-name tie-break: the bare key wins over date-stamped variants.
    keys = {
        "claude-opus-4-8",
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert _best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8"


def test_newest_date_wins_without_bare_key():
    # Alphabetic-desc tie-break among equal prefixes: newer date suffix wins.
    keys = {
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert _best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8-20260514"


def test_no_cross_model_match():
    # Neither string is a prefix of the other, so distinct models never match.
    keys = {"claude-opus-4-5", "claude-opus-4-7"}
    assert _best_prefix_key("claude-opus-4-8", keys) is None


def test_longest_prefix_wins():
    # A date-stamped request resolves to the most specific base key available.
    keys = {"claude-opus-4", "claude-opus-4-8"}
    assert _best_prefix_key("claude-opus-4-8-20260514", keys) == "claude-opus-4-8"


def test_empty_keys():
    assert _best_prefix_key("claude-opus-4-8", []) is None


# --- PriceSource round-trip ---


class _FakeProvider:
    def __init__(self, flat=None, tiered=None):
        self._flat = flat or {}
        self._tiered = tiered

    def get_pricing(self):
        return self._flat

    def get_tiered_pricing(self):
        return self._tiered


def test_date_stamped_request_resolves_to_base_tier(monkeypatch):
    rates = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}
    fake = _FakeProvider(flat={"claude-opus-4-8": rates})
    monkeypatch.setattr("agent_wrap.commands.stats.get_provider", lambda name: fake)

    prices = PriceSource()
    tiers = prices.get_pricing("bedrock", "us.anthropic.claude-opus-4-8-20260514")

    assert tiers is not None
    assert len(tiers) == 1
    assert tiers[0]["in"] == 5.5
    assert tiers[0]["max_in"] == float("inf")


def test_unknown_model_returns_none(monkeypatch):
    rates = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}
    fake = _FakeProvider(flat={"claude-opus-4-8": rates})
    monkeypatch.setattr("agent_wrap.commands.stats.get_provider", lambda name: fake)

    prices = PriceSource()
    assert prices.get_pricing("bedrock", "claude-opus-4-5") is None


# --- known-zero vs unknown cost (all-errors project) ---


def _success_rec(model="claude-opus-4-8"):
    return {
        "status": "success",
        "model": model,
        "timing": {"start": 1_700_000_000.0},
        "response": {
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    }


def _make_prices(monkeypatch, *, priced):
    rates = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}
    flat = {"claude-opus-4-8": rates} if priced else {}
    fake = _FakeProvider(flat=flat)
    monkeypatch.setattr("agent_wrap.commands.stats.get_provider", lambda name: fake)
    return PriceSource()


def test_errored_only_project_costs_known_zero(monkeypatch):
    # A project whose every request errored out is never costed, leaving a
    # *known* zero — the bucket must not be flagged unknown (no spurious "?").
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    rec = {"status": "failure", "model": "claude-opus-4-8"}
    assert _cost_record(rec, "bedrock", prices, by_day) is None
    assert by_day == {}  # nothing accumulated

    # A bucket that never saw a billable request is a known zero.
    b = Bucket()
    assert b.cost == 0.0
    assert b.cost_unknown is False


def test_successful_request_without_price_marks_unknown(monkeypatch):
    # A billable request whose model has no known price flags the bucket
    # unknown, so the cost still renders as "?".
    prices = _make_prices(monkeypatch, priced=False)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    _cost_record(_success_rec(), "bedrock", prices, by_day)
    # day_key is host-local; just grab the single accumulated bucket.
    (bucket,) = next(iter(by_day.values())).values()
    assert bucket.cost_unknown is True


def test_successful_request_with_price_known_cost(monkeypatch):
    # A billable request with a known price accumulates a positive, known cost.
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    _cost_record(_success_rec(), "bedrock", prices, by_day)
    (bucket,) = next(iter(by_day.values())).values()
    assert bucket.cost > 0.0
    assert bucket.cost_unknown is False


# --- .agent_stats_leaf grouping in _aggregate_projects ---------------------


def _write_session_log(project: Path, session_id: str, records: list[dict]) -> None:
    sdir = project / ".claude" / "litellm-logs" / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_aggregate_projects_merges_marked_group(monkeypatch, tmp_path: Path):
    """Two projects under a .agent_stats_leaf marker yield a single named row."""
    prices = _make_prices(monkeypatch, priced=True)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".agent_stats_leaf").write_text("batch-feb\n", encoding="utf-8")

    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session_log(a, "s1", [_success_rec()])
    _write_session_log(b, "s2", [_success_rec()])

    rows, _totals, _by_day = _aggregate_projects([a, b], prices)
    assert len(rows) == 1
    row = rows[0]
    assert row["path"] == runs
    assert row["name"] == "batch-feb"
    assert row["transient"] is True
    assert row["sessions"] == 2
    assert row["total"].msgs == 2


def test_aggregate_projects_empty_marker_is_transient(monkeypatch, tmp_path: Path):
    """An empty .agent_stats_leaf still flags the group transient (dir-named)."""
    prices = _make_prices(monkeypatch, priced=True)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".agent_stats_leaf").write_text("", encoding="utf-8")

    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session_log(a, "s1", [_success_rec()])
    _write_session_log(b, "s2", [_success_rec()])

    rows, _totals, _by_day = _aggregate_projects([a, b], prices)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "runs"
    assert row["transient"] is True

    # The rendered tree must flag the group with a trailing " *".
    labels = [dr.label for dr in flatten_tree(build_project_tree(rows))]
    assert any(label.rstrip().endswith("runs *") for label in labels)


def test_aggregate_projects_keeps_unmarked_separate(monkeypatch, tmp_path: Path):
    """Without a marker each project remains its own row (regression guard)."""
    prices = _make_prices(monkeypatch, priced=True)
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session_log(a, "s1", [_success_rec()])
    _write_session_log(b, "s2", [_success_rec()])

    rows, _totals, _by_day = _aggregate_projects([a, b], prices)
    assert {r["name"] for r in rows} == {"proj-a", "proj-b"}
    assert all(r["transient"] is False for r in rows)


# --- orphaned central logs -------------------------------------------------


def _write_central_log(
    tool_dir: Path, hash_name: str, session_id: str, records: list[dict]
) -> Path:
    """Write a central <hash> log dir directly (no project symlink points at it)."""
    sdir = tool_dir / "litellm-logs" / hash_name / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return tool_dir / "litellm-logs" / hash_name


def test_collect_orphaned_folds_into_totals(monkeypatch, tmp_path: Path):
    """Orphaned usage is summarized and folded into the passed-in totals."""
    prices = _make_prices(monkeypatch, priced=True)
    tool_dir = tmp_path / "tool"
    _write_central_log(tool_dir, "hashB", "s2", [_success_rec(), _success_rec()])

    # Plain dicts, matching what _aggregate_projects returns — an orphaned model
    # not already present must be created on demand, not raise KeyError.
    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}

    orphaned = _collect_orphaned(tool_dir, [], prices, totals_by_model, totals_by_day_by_model)
    assert orphaned is not None
    assert orphaned["sessions"] == 1
    assert orphaned["total"].msgs == 2
    # The two requests were folded into the global per-model totals.
    assert sum(b.msgs for b in totals_by_model.values()) == 2


def test_collect_orphaned_none_when_all_reachable(monkeypatch, tmp_path: Path):
    """A central dir reachable from a registered project is not orphaned."""
    prices = _make_prices(monkeypatch, priced=True)
    tool_dir = tmp_path / "tool"
    hash_a = _write_central_log(tool_dir, "hashA", "s1", [_success_rec()])

    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(hash_a, target_is_directory=True)

    orphaned = _collect_orphaned(tool_dir, [project], prices, {}, {})
    assert orphaned is None


def test_render_includes_orphaned_row_without_star(monkeypatch, tmp_path: Path):
    """render() shows an <orphaned> row, and it carries no ` *` transient marker."""
    prices = _make_prices(monkeypatch, priced=True)
    tool_dir = tmp_path / "tool"
    _write_central_log(tool_dir, "hashB", "s2", [_success_rec()])

    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}
    orphaned = _collect_orphaned(tool_dir, [], prices, totals_by_model, totals_by_day_by_model)

    out = render([], totals_by_model, totals_by_day_by_model, 30, orphaned=orphaned)
    assert "<orphaned>" in out
    assert "<orphaned> *" not in out


def test_render_without_orphaned_has_no_row(monkeypatch):
    """When orphaned is None, no <orphaned> row appears."""
    out = render([], {}, {}, 30, orphaned=None)
    assert "<orphaned>" not in out
