# This file has been created with the assistance of an AI tool.
"""Tests for the `stats` subcommand's model→pricing matching."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING

import agent_wrap.commands.stats as stats_mod
from agent_wrap.commands.stats import (
    PriceSource,
    _aggregate_projects,
    _best_prefix_key,
    _collect_orphaned,
    _cost_record,
    _scan_dirs,
    _scan_logs_dir,
    _usage_source,
    _usage_unrecorded,
    extract_usage,
    plan_pool,
    render,
    render_source_breakdown,
    request_cache_ttl,
)
from agent_wrap.lib.buckets import Bucket
from agent_wrap.lib.tree import build_project_tree, flatten_tree
from agent_wrap.lib.usage_args import parse_usage_args

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
    assert _cost_record(rec, "bedrock", prices, by_day) == (False, None)
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


def test_unrecorded_usage_string_response_counted(monkeypatch):
    # A legacy success record whose response is a bare "<Response ...>" string has
    # no usage; it must be counted as unrecorded (not silently a $0 row).
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "response": "<Response [200 OK]>",
    }
    _cost_record(rec, "bedrock", prices, by_day)
    (bucket,) = next(iter(by_day.values())).values()
    assert bucket.unrecorded == 1
    assert bucket.cost == 0.0


def test_unrecorded_usage_unrecoverable_marker_counted(monkeypatch):
    # A post-fix record the callback tagged "unrecoverable" is likewise counted.
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "response": {"_usage_source": "unrecoverable", "_raw_response": "<Response [200 OK]>"},
    }
    _cost_record(rec, "bedrock", prices, by_day)
    (bucket,) = next(iter(by_day.values())).values()
    assert bucket.unrecorded == 1


def test_normal_success_not_counted_unrecorded(monkeypatch):
    # A normal priced success must not be flagged unrecorded.
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    _cost_record(_success_rec(), "bedrock", prices, by_day)
    (bucket,) = next(iter(by_day.values())).values()
    assert bucket.unrecorded == 0


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

    rows, _totals, _by_day, _by_source = _aggregate_projects([a, b], prices)
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

    rows, _totals, _by_day, _by_source = _aggregate_projects([a, b], prices)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "runs"
    assert row["transient"] is True

    # The rendered tree must flag the group transient (accented in color by the
    # renderer) and label it with the group name — no " *" text marker.
    display = flatten_tree(build_project_tree(rows))
    group = next(dr for dr in display if dr.label.rstrip().endswith("runs"))
    assert group.transient is True
    assert " *" not in group.label


def test_aggregate_projects_keeps_unmarked_separate(monkeypatch, tmp_path: Path):
    """Without a marker each project remains its own row (regression guard)."""
    prices = _make_prices(monkeypatch, priced=True)
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session_log(a, "s1", [_success_rec()])
    _write_session_log(b, "s2", [_success_rec()])

    rows, _totals, _by_day, _by_source = _aggregate_projects([a, b], prices)
    assert {r["name"] for r in rows} == {"proj-a", "proj-b"}
    assert all(r["transient"] is False for r in rows)


# --- windowing: range filtering at the data layer --------------------------


def _day_epoch(day: str) -> float:
    """Local-midnight epoch seconds for a YYYY-MM-DD day string."""
    return datetime.strptime(day, "%Y-%m-%d").astimezone().timestamp()


def _dated_rec(day: str, model="claude-opus-4-8"):
    """Build a success record whose timing.start lands on the given day (local)."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": _day_epoch(day)},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }


def test_cost_record_skips_out_of_range(monkeypatch):
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    # In range → accumulated.
    acc, _ts = _cost_record(
        _dated_rec("2026-06-15"),
        "bedrock",
        prices,
        by_day,
        from_iso="2026-06-01",
        until_iso="2026-06-30",
    )
    assert acc is True
    # Out of range → skipped, nothing accumulated.
    acc, ts = _cost_record(
        _dated_rec("2026-05-01"),
        "bedrock",
        prices,
        by_day,
        from_iso="2026-06-01",
        until_iso="2026-06-30",
    )
    assert (acc, ts) == (False, None)
    assert set(by_day) == {"2026-06-15"}


def test_aggregate_projects_windows_sessions_and_totals(monkeypatch, tmp_path: Path):
    # A project with one in-window session and one out-of-window session: only the
    # in-window one is counted and totalled.
    prices = _make_prices(monkeypatch, priced=True)
    proj = tmp_path / "proj"
    _write_session_log(proj, "in", [_dated_rec("2026-06-15")])
    _write_session_log(proj, "out", [_dated_rec("2026-01-01")])

    rows, _totals, by_day, _by_source = _aggregate_projects(
        [proj], prices, from_iso="2026-06-01", until_iso="2026-06-30"
    )
    assert len(rows) == 1
    assert rows[0]["sessions"] == 1  # the out-of-window session is not counted
    assert rows[0]["total"].msgs == 1
    assert set(by_day) == {"2026-06-15"}


def test_file_culling_skips_old_mtime(monkeypatch, tmp_path: Path):
    # A log file whose mtime predates the lower bound is skipped unread, even though
    # it would parse to in-range records (proving the metadata short-circuit).
    prices = _make_prices(monkeypatch, priced=True)
    logs = tmp_path / ".claude" / "litellm-logs"
    sdir = logs / "litellm-bedrock" / "s1"
    sdir.mkdir(parents=True)
    msg = sdir / "messages.jsonl"
    msg.write_text(json.dumps(_dated_rec("2026-06-15")) + "\n", encoding="utf-8")

    # Back-date the file mtime to well before the window.
    old = _day_epoch("2026-01-01")
    os.utime(msg, (old, old))

    sessions, _last_ts, by_day, _by_source = _scan_logs_dir(
        logs, prices, from_iso="2026-06-01", until_iso="2026-06-30"
    )
    assert sessions == 0  # culled: not read, not counted
    assert by_day == {}


def test_file_culling_keeps_recent_mtime_but_filters_records(monkeypatch, tmp_path: Path):
    # A recently-written file is parsed; its out-of-range records are filtered by
    # day_in_range, never counted (only the upper bound, not cullable via mtime).
    prices = _make_prices(monkeypatch, priced=True)
    logs = tmp_path / ".claude" / "litellm-logs"
    sdir = logs / "litellm-bedrock" / "s1"
    sdir.mkdir(parents=True)
    msg = sdir / "messages.jsonl"
    with msg.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_dated_rec("2026-06-15")) + "\n")  # in range
        f.write(json.dumps(_dated_rec("2026-07-15")) + "\n")  # after upper bound

    sessions, _last_ts, by_day, _by_source = _scan_logs_dir(
        logs, prices, from_iso="2026-06-01", until_iso="2026-06-30"
    )
    assert sessions == 1
    assert set(by_day) == {"2026-06-15"}


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


def test_render_includes_orphaned_row(monkeypatch, tmp_path: Path):
    """render() shows an <orphaned> row (accented in color, no text marker)."""
    prices = _make_prices(monkeypatch, priced=True)
    tool_dir = tmp_path / "tool"
    _write_central_log(tool_dir, "hashB", "s2", [_success_rec()])

    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}
    orphaned = _collect_orphaned(tool_dir, [], prices, totals_by_model, totals_by_day_by_model)

    out = render([], totals_by_day_by_model, None, None, orphaned=orphaned)
    assert "<orphaned>" in out
    assert "<orphaned> *" not in out


def test_render_without_orphaned_has_no_row(monkeypatch):
    """When orphaned is None, no <orphaned> row appears."""
    out = render([], {}, None, None, orphaned=None)
    assert "<orphaned>" not in out


# --- request_cache_ttl: extracting the cache TTL tier from a request ---------


def _request_with_ttls(*ttls: str | None) -> dict:
    """Build a request whose system blocks carry one cache_control per `ttls`."""
    system = []
    for ttl in ttls:
        cc: dict = {"type": "ephemeral"}
        if ttl is not None:
            cc["ttl"] = ttl
        system.append({"type": "text", "text": "x", "cache_control": cc})
    return {"body": {"data": {"system": system}}}


def test_request_cache_ttl_default_is_5m():
    # A bare {"type": "ephemeral"} marker (no ttl) is the 5-minute default.
    assert request_cache_ttl(_request_with_ttls(None, None)) == "5m"


def test_request_cache_ttl_one_hour():
    assert request_cache_ttl(_request_with_ttls("1h", "1h")) == "1h"


def test_request_cache_ttl_mixed():
    assert request_cache_ttl(_request_with_ttls(None, "1h")) == "mixed"


def test_request_cache_ttl_none_without_markers():
    assert request_cache_ttl({"body": {"data": {"system": []}}}) is None
    assert request_cache_ttl(None) is None
    assert request_cache_ttl({}) is None


# --- extract_usage: request-TTL attribution of a flat cache-write total -------


def _flat_cache_response(cw: int = 1000) -> dict:
    """Build a response carrying only the flat cache_creation_input_tokens (Bedrock)."""
    return {
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 100,
            "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": 0,
        }
    }


def test_extract_usage_attributes_flat_total_to_1h():
    usage = extract_usage(_flat_cache_response(1000), "1h")
    assert usage["cache_creation"] == {"ephemeral_1h_input_tokens": 1000}


def test_extract_usage_attributes_flat_total_to_5m():
    usage = extract_usage(_flat_cache_response(1000), "5m")
    assert usage["cache_creation"] == {"ephemeral_5m_input_tokens": 1000}


def test_extract_usage_defaults_to_5m_without_request_ttl():
    # No request TTL → no attribution; the flat total is left for the downstream
    # 5m fallback in _cost_for_tiers / Bucket.add (unchanged legacy behavior).
    usage = extract_usage(_flat_cache_response(1000))
    assert usage["cache_creation"] == {}
    assert usage["cache_creation_input_tokens"] == 1000


def test_extract_usage_trusts_response_split_over_request_ttl():
    # When the response already reports the ephemeral split, the request TTL is
    # ignored — the response is authoritative.
    response = {
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 100,
            "cache_creation_input_tokens": 1000,
            "ephemeral_5m_input_tokens": 600,
            "ephemeral_1h_input_tokens": 400,
        }
    }
    usage = extract_usage(response, "1h")
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 600,
        "ephemeral_1h_input_tokens": 400,
    }


def test_extract_usage_reads_nested_cache_creation_split():
    # Anthropic's documented shape nests the split under usage.cache_creation.
    # That response-provided split wins over a conflicting request TTL.
    response = {
        "usage": {
            "input_tokens": 2048,
            "cache_read_input_tokens": 1800,
            "cache_creation_input_tokens": 248,
            "output_tokens": 503,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 148,
                "ephemeral_1h_input_tokens": 100,
            },
        }
    }
    usage = extract_usage(response, "5m")
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 148,
        "ephemeral_1h_input_tokens": 100,
    }


def test_extract_usage_mixed_ttl_falls_back_to_5m_and_warns(monkeypatch, capsys):
    monkeypatch.setattr(stats_mod, "_mixed_cache_ttl_warned", False)
    usage = extract_usage(_flat_cache_response(1000), "mixed")
    # Unattributed: left flat for the 5m fallback, and warned once.
    assert usage["cache_creation"] == {}
    assert usage["cache_creation_input_tokens"] == 1000
    assert "mixed 5m and 1h cache TTLs" in capsys.readouterr().err

    # Second call is silent (warn-once).
    extract_usage(_flat_cache_response(1000), "mixed")
    assert capsys.readouterr().err == ""


def test_cost_record_uses_request_ttl_for_1h_rate(monkeypatch):
    # End-to-end through _cost_record: a 1h request bills cache writes at the
    # higher 1h rate, proving the TTL threads through to the cost math.
    prices = _make_prices(monkeypatch, priced=True)  # cw_5m=6.875, cw_1h=11.0
    rec_1h = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "request": _request_with_ttls("1h"),
        "response": _flat_cache_response(1_000_000),
    }
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    _cost_record(rec_1h, "bedrock", prices, by_day)
    (bucket_1h,) = next(iter(by_day.values())).values()
    assert bucket_1h.cw_1h == 1_000_000
    assert bucket_1h.cw_5m == 0

    rec_5m = {**rec_1h, "request": _request_with_ttls(None)}
    by_day_5m: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    _cost_record(rec_5m, "bedrock", prices, by_day_5m)
    (bucket_5m,) = next(iter(by_day_5m.values())).values()
    assert bucket_5m.cw_5m == 1_000_000
    assert bucket_5m.cw_1h == 0
    # Higher TTL ⇒ strictly higher cost for the same token count.
    assert bucket_1h.cost > bucket_5m.cost


# --- _usage_source: provenance classification --------------------------------


def _slo_rec(model="claude-opus-4-8"):
    """Build a success record whose usage was recovered from the standard logging object."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": 1_700_000_000.0},
        "response": {
            "_usage_source": "standard_logging_object",
            "usage": {"prompt_tokens": 800, "completion_tokens": 200},
        },
    }


def _unrecoverable_rec(model="claude-opus-4-8"):
    """Build a success record the callback could not recover any usage for."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": 1_700_000_000.0},
        "response": {"_usage_source": "unrecoverable", "_raw_response": "<Response [200 OK]>"},
    }


def test_usage_source_native():
    # A parsed response dict with no _usage_source key is native.
    assert _usage_source(_success_rec()) == "native"


def test_usage_source_standard_logging_object():
    assert _usage_source(_slo_rec()) == "standard_logging_object"


def test_usage_source_unrecoverable_marker():
    assert _usage_source(_unrecoverable_rec()) == "unrecoverable"


def test_usage_source_legacy_string():
    # A bare legacy "<Response ...>" string carries no usage → unrecoverable.
    rec = {"status": "success", "model": "claude-opus-4-8", "response": "<Response [200 OK]>"}
    assert _usage_source(rec) == "unrecoverable"


def test_usage_unrecorded_agrees_with_source():
    # Drift guard: _usage_unrecorded is exactly the "unrecoverable" classification.
    assert _usage_unrecorded(_success_rec()) is False
    assert _usage_unrecorded(_slo_rec()) is False
    assert _usage_unrecorded(_unrecoverable_rec()) is True


def test_cost_record_populates_by_source(monkeypatch):
    # _cost_record routes the same usage/cost into by_day_by_source[day][source].
    prices = _make_prices(monkeypatch, priced=True)
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    by_source: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    _cost_record(_slo_rec(), "bedrock", prices, by_day, by_source)
    (day_sources,) = by_source.values()
    assert set(day_sources) == {"standard_logging_object"}
    assert day_sources["standard_logging_object"].msgs == 1


def test_aggregate_projects_returns_per_source_totals(monkeypatch, tmp_path: Path):
    # A project mixing all three sources yields a per-source breakdown.
    prices = _make_prices(monkeypatch, priced=True)
    proj = tmp_path / "proj"
    _write_session_log(proj, "s1", [_success_rec(), _slo_rec(), _unrecoverable_rec()])

    _rows, _totals, _by_day, by_source = _aggregate_projects([proj], prices)

    # All three records land on the same day (shared timing.start); flatten it.
    merged: dict[str, Bucket] = defaultdict(Bucket)
    for day_sources in by_source.values():
        for source, b in day_sources.items():
            merged[source].merge(b)

    assert merged["native"].msgs == 1
    assert merged["standard_logging_object"].msgs == 1
    assert merged["unrecoverable"].msgs == 1
    assert merged["unrecoverable"].unrecorded == 1


# --- render_source_breakdown -------------------------------------------------


def _source_bucket(msgs: int, *, in_: int = 0) -> Bucket:
    b = Bucket()
    for _ in range(msgs):
        b.add({"input_tokens": in_}, 0.0)
    return b


def test_render_source_breakdown_lists_active_sources():
    by_day_by_source = {
        "2026-06-29": {
            "native": _source_bucket(3, in_=1000),
            "standard_logging_object": _source_bucket(2, in_=500),
            "unrecoverable": _source_bucket(1),  # zero tokens, still counted
        }
    }
    out = render_source_breakdown(by_day_by_source, None, None)
    assert "Usage source breakdown (all time):" in out
    assert "native" in out
    assert "standard_logging_object" in out
    assert "unrecoverable" in out
    assert "TOTAL" in out


def test_render_source_breakdown_omits_zero_msg_sources():
    by_day_by_source = {"2026-06-29": {"native": _source_bucket(2, in_=100)}}
    out = render_source_breakdown(by_day_by_source, None, None)
    assert "native" in out
    assert "standard_logging_object" not in out


def test_render_source_breakdown_empty_when_no_activity():
    assert render_source_breakdown({}, None, None) == ""


def test_render_source_breakdown_merges_supplied_days():
    # The source dict is already windowed at scan time, so render merges every day
    # present (the range only drives the title). Both days appear here.
    by_day_by_source = {
        "2026-06-01": {"unrecoverable": _source_bucket(1)},
        "2026-06-29": {"native": _source_bucket(1, in_=1)},
    }
    out = render_source_breakdown(by_day_by_source, "2026-06-01", "2026-06-29")
    assert "Usage source breakdown (2026-06-01 … 2026-06-29):" in out
    assert "unrecoverable" in out
    assert "native" in out


# --- verbose arg parsing -----------------------------------------------------


def test_parse_usage_args_verbose_short_flag(tmp_path: Path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg), "-v"], usage_line="u", usage_text="u")
    assert parsed is not None
    assert parsed.verbose is True


def test_parse_usage_args_verbose_long_flag(tmp_path: Path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg), "--verbose"], usage_line="u", usage_text="u")
    assert parsed is not None
    assert parsed.verbose is True


def test_parse_usage_args_verbose_defaults_false(tmp_path: Path):
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    parsed = parse_usage_args([str(reg)], usage_line="u", usage_text="u")
    assert parsed is not None
    assert parsed.verbose is False


# --- lazy request_cache_ttl --------------------------------------------------


def test_cost_record_skips_ttl_walk_when_response_has_split(monkeypatch):
    # When the response already carries an ephemeral split, the request-side TTL
    # is never needed — so _cost_record must not walk the request body for it.
    prices = _make_prices(monkeypatch, priced=True)
    calls = {"n": 0}
    real = stats_mod.request_cache_ttl
    monkeypatch.setattr(
        stats_mod,
        "request_cache_ttl",
        lambda req: (calls.__setitem__("n", calls["n"] + 1), real(req))[1],
    )
    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "request": _request_with_ttls("1h"),
        "response": {
            "usage": {
                "prompt_tokens": 5000,
                "completion_tokens": 100,
                "cache_creation_input_tokens": 1000,
                "ephemeral_5m_input_tokens": 600,
                "ephemeral_1h_input_tokens": 400,
            }
        },
    }
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    _cost_record(rec, "bedrock", prices, by_day)
    assert calls["n"] == 0


def test_cost_record_skips_ttl_walk_without_cache_writes(monkeypatch):
    # No cache-write tokens at all → the TTL is irrelevant, so it is not consulted.
    prices = _make_prices(monkeypatch, priced=True)
    calls = {"n": 0}
    real = stats_mod.request_cache_ttl
    monkeypatch.setattr(
        stats_mod,
        "request_cache_ttl",
        lambda req: (calls.__setitem__("n", calls["n"] + 1), real(req))[1],
    )
    rec = {**_success_rec(), "request": _request_with_ttls("1h")}
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    _cost_record(rec, "bedrock", prices, by_day)
    assert calls["n"] == 0


def test_cost_record_consults_ttl_for_flat_bedrock_total(monkeypatch):
    # The Bedrock case — a flat cache-write total with no ephemeral split — is the
    # one case that *does* need the request TTL, so it must be consulted.
    prices = _make_prices(monkeypatch, priced=True)
    calls = {"n": 0}
    real = stats_mod.request_cache_ttl
    monkeypatch.setattr(
        stats_mod,
        "request_cache_ttl",
        lambda req: (calls.__setitem__("n", calls["n"] + 1), real(req))[1],
    )
    rec = {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "request": _request_with_ttls("1h"),
        "response": _flat_cache_response(1000),
    }
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    _cost_record(rec, "bedrock", prices, by_day)
    assert calls["n"] == 1
    # And the TTL still threads through: the 1h request bills the writes as 1h.
    (bucket,) = next(iter(by_day.values())).values()
    assert bucket.cw_1h == 1000
    assert bucket.cw_5m == 0


# --- plan_pool sizing heuristic ----------------------------------------------


def test_plan_pool_caps_workers_at_eight(monkeypatch):
    # Many cores and many files still cap at 8 workers (decode saturates there).
    monkeypatch.setattr(stats_mod.os, "cpu_count", lambda: 64)
    workers, chunksize = plan_pool(10_000)
    assert workers == 8
    assert 1 <= chunksize <= 8


def test_plan_pool_scales_down_on_single_core(monkeypatch):
    monkeypatch.setattr(stats_mod.os, "cpu_count", lambda: 1)
    workers, chunksize = plan_pool(10_000)
    assert workers == 1
    assert 1 <= chunksize <= 8


def test_plan_pool_scales_workers_to_file_count(monkeypatch):
    # Few files shouldn't fork a full fleet: ceil(nfiles/16) bounds the workers.
    monkeypatch.setattr(stats_mod.os, "cpu_count", lambda: 64)
    workers, _chunksize = plan_pool(20)
    assert workers == 2  # ceil(20/16)


def test_plan_pool_chunksize_in_clamp_range(monkeypatch):
    # Across a wide range of inputs the chunksize stays within the tuned [1, 8].
    monkeypatch.setattr(stats_mod.os, "cpu_count", lambda: 20)
    for nfiles in (1, 20, 80, 200, 800, 4430, 50_000):
        workers, chunksize = plan_pool(nfiles)
        assert workers >= 1
        assert 1 <= chunksize <= 8


def test_plan_pool_handles_unknown_cpu_count(monkeypatch):
    # os.cpu_count() can return None; the heuristic must still yield >=1 worker.
    monkeypatch.setattr(stats_mod.os, "cpu_count", lambda: None)
    workers, chunksize = plan_pool(1000)
    assert workers == 1
    assert 1 <= chunksize <= 8


# --- _scan_dirs: serial / parallel equivalence -------------------------------


def _seed_many_dirs(tool_dir: Path, n: int, records_per: int) -> list[Path]:
    """Create `n` central <hash> log dirs, each one session of `records_per` recs."""
    dirs = []
    for i in range(n):
        recs = [_dated_rec("2026-06-15") for _ in range(records_per)]
        dirs.append(_write_central_log(tool_dir, f"hash{i:04d}", "s1", recs))
    return dirs


def test_scan_dirs_serial_matches_scan_logs_dir(monkeypatch, tmp_path: Path):
    # The serial _scan_dirs path must fold to the same result _scan_logs_dir gives.
    prices = _make_prices(monkeypatch, priced=True)
    dirs = _seed_many_dirs(tmp_path / "tool", 3, records_per=2)

    monkeypatch.setattr(stats_mod, "_PARALLEL_MIN_FILES", 10**9)  # force serial
    cache = _scan_dirs(dirs, from_iso=None, until_iso=None)
    for d in dirs:
        expect = _scan_logs_dir(d, prices, from_iso=None, until_iso=None)
        got = cache[d]
        assert got[0] == expect[0]  # sessions
        assert {m: b.msgs for v in got[2].values() for m, b in v.items()} == {
            m: b.msgs for v in expect[2].values() for m, b in v.items()
        }


def test_scan_dirs_parallel_matches_serial(monkeypatch, tmp_path: Path):
    # Forcing the pool path must yield byte-identical aggregates to the serial one,
    # proving parallelism doesn't change results. Enough files to exercise chunks.
    _make_prices(monkeypatch, priced=True)
    dirs = _seed_many_dirs(tmp_path / "tool", 80, records_per=2)

    monkeypatch.setattr(stats_mod, "_PARALLEL_MIN_FILES", 10**9)
    serial = _scan_dirs(dirs, from_iso=None, until_iso=None)
    monkeypatch.setattr(stats_mod, "_PARALLEL_MIN_FILES", 1)
    parallel = _scan_dirs(dirs, from_iso=None, until_iso=None)

    def norm(cache: dict) -> dict:
        out = {}
        for d, (sess, ts, by_day, _by_src) in cache.items():
            out[str(d)] = (
                sess,
                ts,
                {
                    day: {
                        m: (b.msgs, b.in_, b.out, b.cw, b.cr, round(b.cost, 9))
                        for m, b in v.items()
                    }
                    for day, v in by_day.items()
                },
            )
        return out

    assert norm(serial) == norm(parallel)


def test_scan_dirs_parallel_totals_match_records(monkeypatch, tmp_path: Path):
    # End-to-end count check on the parallel path: every record is costed once.
    _make_prices(monkeypatch, priced=True)
    dirs = _seed_many_dirs(tmp_path / "tool", 80, records_per=3)

    monkeypatch.setattr(stats_mod, "_PARALLEL_MIN_FILES", 1)  # force parallel
    cache = _scan_dirs(dirs, from_iso=None, until_iso=None)
    total_msgs = sum(
        b.msgs for _, _, by_day, _ in cache.values() for v in by_day.values() for b in v.values()
    )
    assert total_msgs == 80 * 3  # no double-counting, no drops
