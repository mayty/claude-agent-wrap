# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_wrap.lib.console import Ansi
from agent_wrap.providers import get_provider

USAGE = "[--refresh] [--days N]"
SUMMARY = "Show token usage stats (reads from .claude/litellm-logs/)"

_BILLION = 1_000_000_000
_MILLION = 1_000_000
_THOUSAND = 1_000

_MODEL_FAMILY_RE_V_FIRST = re.compile(
    r"claude[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)[-\s.]*(?P<tier>opus|sonnet|haiku)",
    re.IGNORECASE,
)
_MODEL_FAMILY_RE_T_FIRST = re.compile(
    r"claude[-\s.]*(?P<tier>opus|sonnet|haiku)[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
_MODEL_CONTEXT_SUFFIX_RE = re.compile(r"\[(?:1m|128k|32k|8k)\]$", re.IGNORECASE)


def color(s: str, code: str) -> str:
    return f"{code}{s}{Ansi.RESET}" if sys.stdout.isatty() else s


def fmt_count(n: int) -> str:
    if n >= _BILLION:
        return f"{n / _BILLION:.2f}G"
    if n >= _MILLION:
        return f"{n / _MILLION:.2f}M"
    if n >= _THOUSAND:
        return f"{n / _THOUSAND:.1f}K"
    return str(n)


def fmt_cost(c: float | None) -> str:
    if c is None:
        return "?"
    return f"${c:.2f}"


def fmt_cost_with_unknown(c: float | None, *, unknown: bool) -> str:
    """Format cost, collapsing '$0.00+?' to just '?'."""
    if c is None or (c == 0.0 and unknown):
        return "?"
    if unknown:
        return f"${c:.2f}+?"
    return f"${c:.2f}"


def normalize_model(model: str) -> str | None:
    """
    Return a canonical 'claude-<tier>-<ver>' key for a session model id.

    Handles the various forms session JSONLs surface:
      claude-opus-4-7
      claude-sonnet-4-5-20250929          (date-stamped snapshot)
      anthropic.claude-opus-4-7-v1:0      (Bedrock model id form)
      Claude Opus 4.7                     (display name)

    Returns None if `model` doesn't look like a Claude release.
    """
    if not model:
        return None
    bare = _DATE_SUFFIX_RE.sub("", model)
    m = _MODEL_FAMILY_RE_T_FIRST.search(bare) or _MODEL_FAMILY_RE_V_FIRST.search(bare)
    if not m:
        return None
    tier = m.group("tier").lower()
    ver = m.group("ver").replace(".", "-")
    return f"claude-{tier}-{ver}"


def extract_usage(response: dict | None) -> dict[str, Any]:
    """Extract and normalize usage dict from LiteLLM response object."""
    if not response or not isinstance(response, dict):
        return {}
    usage = response.get("usage")
    if not usage or not isinstance(usage, dict):
        return {}

    cache_creation = {}
    if "ephemeral_5m_input_tokens" in usage:
        cache_creation["ephemeral_5m_input_tokens"] = usage["ephemeral_5m_input_tokens"]
    if "ephemeral_1h_input_tokens" in usage:
        cache_creation["ephemeral_1h_input_tokens"] = usage["ephemeral_1h_input_tokens"]

    # LiteLLM standardizes to prompt_tokens/completion_tokens, but some
    # providers or older versions might use input_tokens/output_tokens.
    in_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    cw_tokens = usage.get("cache_creation_input_tokens") or 0
    cr_tokens = usage.get("cache_read_input_tokens") or 0

    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_creation_input_tokens": cw_tokens,
        "cache_read_input_tokens": cr_tokens,
        "cache_creation": cache_creation,
    }


def cost_for_model(display_model: str, usage: dict, provider_prices: dict) -> float | None:
    """Calculate cost for a model using the provider's pricing table."""
    if not any(usage.values()):
        return 0.0

    parts = display_model.split("/", 1)
    if len(parts) < 2:  # noqa: PLR2004
        return None
    provider_name, norm_model = parts[0], parts[1]

    prices = provider_prices.get(provider_name, {})
    p = prices.get(norm_model)
    if p is None:
        return None

    return (
        usage["in"] * p["in"] / 1_000_000
        + usage["out"] * p["out"] / 1_000_000
        + usage["cw_5m"] * p["cw_5m"] / 1_000_000
        + usage["cw_1h"] * p["cw_1h"] / 1_000_000
        + usage["cr"] * p["cr"] / 1_000_000
    )


def cost_for_request(
    provider_name: str,
    model: str,
    usage: dict,
    provider_tiered_prices: dict,
) -> float | None:
    """Calculate cost for a single request using tiered pricing rules."""
    in_tokens = usage.get("input_tokens", 0) or 0
    out_tokens = usage.get("output_tokens", 0) or 0
    cr_tokens = usage.get("cache_read_input_tokens", 0) or 0

    cc = usage.get("cache_creation") or {}
    cw_5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
    cw_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    if not (cw_5m or cw_1h):
        cw_5m = usage.get("cache_creation_input_tokens", 0) or 0

    if not (in_tokens or out_tokens or cw_5m or cw_1h or cr_tokens):
        return 0.0

    # Normalize model name by stripping context suffixes (e.g., [1m], [128k])
    norm_model = _MODEL_CONTEXT_SUFFIX_RE.sub("", model)

    tiered_model = provider_tiered_prices.get(provider_name, {}).get(norm_model)
    if not tiered_model or "tiers" not in tiered_model:
        return None

    # Find the applicable tier based on TOTAL input tokens
    applicable_tier = None
    for tier in tiered_model["tiers"]:
        if in_tokens <= tier["max_in"]:
            applicable_tier = tier
            break

    # Fallback to the highest tier if input tokens exceed all defined max_in
    if applicable_tier is None:
        applicable_tier = tiered_model["tiers"][-1]

    # Fresh input tokens are total input tokens minus cached tokens
    # to avoid double-charging cached tokens at the base rate.
    fresh_in_tokens = max(0, in_tokens - cr_tokens)

    return (
        fresh_in_tokens * applicable_tier["in"] / 1_000_000
        + out_tokens * applicable_tier["out"] / 1_000_000
        + cw_5m * applicable_tier["cw_5m"] / 1_000_000
        + cw_1h * applicable_tier["cw_1h"] / 1_000_000
        + cr_tokens * applicable_tier["cr"] / 1_000_000
    )


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


class Bucket:
    __slots__ = ("cost", "cr", "cw_1h", "cw_5m", "in_", "msgs", "out")

    def __init__(self) -> None:
        self.msgs = 0
        self.in_ = 0
        self.out = 0
        self.cw_5m = 0
        self.cw_1h = 0
        self.cr = 0
        self.cost = 0.0

    def add(self, usage: dict, request_cost: float = 0.0) -> None:
        self.msgs += 1
        self.in_ += usage.get("input_tokens", 0) or 0
        self.out += usage.get("output_tokens", 0) or 0
        cc = usage.get("cache_creation") or {}
        h5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        h1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if h5 or h1:
            self.cw_5m += h5
            self.cw_1h += h1
        else:
            self.cw_5m += usage.get("cache_creation_input_tokens", 0) or 0
        self.cr += usage.get("cache_read_input_tokens", 0) or 0
        self.cost += request_cost

    def merge(self, other: Bucket) -> None:
        self.msgs += other.msgs
        self.in_ += other.in_
        self.out += other.out
        self.cw_5m += other.cw_5m
        self.cw_1h += other.cw_1h
        self.cr += other.cr
        self.cost += other.cost

    @property
    def cw(self) -> int:
        return self.cw_5m + self.cw_1h

    def usage_dict(self) -> dict:
        return {
            "in": self.in_,
            "out": self.out,
            "cw_5m": self.cw_5m,
            "cw_1h": self.cw_1h,
            "cr": self.cr,
        }


class Node:
    """One node in the path trie used to render the per-project tree."""

    __slots__ = (
        "children",
        "name",
        "row",
        "subtree_bucket",
        "subtree_known_cost",
        "subtree_last_ts",
        "subtree_project_count",
        "subtree_sessions",
        "subtree_unknown",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, Node] = {}
        self.row: dict | None = None
        self.subtree_bucket = Bucket()
        self.subtree_known_cost = 0.0
        self.subtree_unknown = False
        self.subtree_sessions = 0
        self.subtree_last_ts: datetime | None = None
        self.subtree_project_count = 0


class DisplayRow:
    __slots__ = (
        "bucket",
        "cost_str",
        "is_structural",
        "label",
        "last_ts",
        "prefix_len",
        "sessions",
    )

    def __init__(  # noqa: PLR0913
        self,
        label: str,
        prefix_len: int,
        *,
        is_structural: bool,
        sessions: int,
        bucket: Bucket,
        last_ts: datetime | None,
        cost_str: str,
    ) -> None:
        self.label = label
        self.prefix_len = prefix_len
        self.is_structural = is_structural
        self.sessions = sessions
        self.bucket = bucket
        self.last_ts = last_ts
        self.cost_str = cost_str


def build_project_tree(rows: list[dict]) -> Node:
    root = Node("/")
    for r in rows:
        parts = Path(r["path"]).parts
        if not parts:
            continue
        segments = parts[1:] if parts[0] == "/" else parts
        cur = root
        for seg in segments:
            if seg not in cur.children:
                cur.children[seg] = Node(seg)
            cur = cur.children[seg]
        cur.row = r

    _compress(root)
    _split_self_rows(root)
    _aggregate(root)
    return root


def _compress(node: Node) -> None:
    new_children: dict[str, Node] = {}
    for child in list(node.children.values()):
        _compress(child)
        while child.row is None and len(child.children) == 1:
            (gc,) = child.children.values()
            gc.name = f"{child.name}/{gc.name}"
            child = gc  # noqa: PLW2901
        new_children[child.name] = child
    node.children = new_children


def _split_self_rows(node: Node) -> None:
    for child in list(node.children.values()):
        _split_self_rows(child)
    if node.row is not None and node.children:
        dot = Node(".")
        dot.row = node.row
        node.row = None
        new_children: dict[str, Node] = {".": dot}
        new_children.update(node.children)
        node.children = new_children


def _aggregate(node: Node) -> None:
    for child in node.children.values():
        _aggregate(child)
        node.subtree_bucket.merge(child.subtree_bucket)
        node.subtree_known_cost += child.subtree_known_cost
        if child.subtree_unknown:
            node.subtree_unknown = True
        node.subtree_sessions += child.subtree_sessions
        node.subtree_project_count += child.subtree_project_count
        if child.subtree_last_ts is not None and (
            node.subtree_last_ts is None or child.subtree_last_ts > node.subtree_last_ts
        ):
            node.subtree_last_ts = child.subtree_last_ts
    if node.row is not None:
        r = node.row
        node.subtree_bucket.merge(r["total"])
        if r["cost"] is None:
            node.subtree_unknown = True
        else:
            node.subtree_known_cost += r["cost"]
        node.subtree_sessions += r["sessions"]
        node.subtree_project_count += 1
        if r["last_ts"] is not None and (
            node.subtree_last_ts is None or r["last_ts"] > node.subtree_last_ts
        ):
            node.subtree_last_ts = r["last_ts"]


def flatten_tree(root: Node) -> list[DisplayRow]:
    out: list[DisplayRow] = []

    def walk(node: Node, ancestors_continue: list[bool]) -> None:
        children = list(node.children.values())
        dot = [c for c in children if c.name == "."]
        leaves = sorted(
            (c for c in children if c.name != "." and not c.children),
            key=lambda c: c.name,
        )
        nodes = sorted(
            (c for c in children if c.name != "." and c.children),
            key=lambda c: (c.subtree_project_count, c.name),
        )
        ordered = dot + leaves + nodes

        for i, child in enumerate(ordered):
            is_last = i == len(ordered) - 1
            connector = "└" if is_last else "├"
            prefix = "".join("│" if cont else " " for cont in ancestors_continue) + connector
            prefix_len = len(prefix)

            label = prefix + child.name
            if child.children:
                label += "/"
            if child.row is not None and not child.row["exists"]:
                label += " (missing)"

            if child.row is not None:
                r = child.row
                cost_str = fmt_cost(r["cost"])
                out.append(
                    DisplayRow(
                        label=label,
                        prefix_len=prefix_len,
                        is_structural=False,
                        sessions=r["sessions"],
                        bucket=r["total"],
                        last_ts=r["last_ts"],
                        cost_str=cost_str,
                    )
                )
            else:
                cost_str = fmt_cost_with_unknown(
                    child.subtree_known_cost, unknown=child.subtree_unknown
                )
                out.append(
                    DisplayRow(
                        label=label,
                        prefix_len=prefix_len,
                        is_structural=True,
                        sessions=child.subtree_sessions,
                        bucket=child.subtree_bucket,
                        last_ts=child.subtree_last_ts,
                        cost_str=cost_str,
                    )
                )

            if child.children:
                walk(child, [*ancestors_continue, not is_last])

    walk(root, [])
    return out


def _process_litellm_record(
    rec: dict,
    provider_name: str,
    buckets: dict[str, dict[str, Bucket]],
    tiered_prices: dict | None,
) -> tuple[datetime | None, float | None]:
    """Process a single LiteLLM log record. Returns (timestamp, request_cost)."""
    if rec.get("status") != "success":
        return None, None

    model = rec.get("model")
    if not model:
        return None, None

    clean_model = model.split("/")[-1] if "/" in model else model
    norm_model = normalize_model(clean_model) or clean_model
    display_model = f"{provider_name}/{norm_model}"

    ts_str = rec.get("ts")
    ts = parse_ts(ts_str)
    day_key = ts.astimezone().strftime("%Y-%m-%d") if ts else "?"

    usage = extract_usage(rec.get("response"))
    request_cost = None
    if tiered_prices is not None:
        request_cost = cost_for_request(
            provider_name, clean_model, usage, {provider_name: tiered_prices}
        )

    bucket = Bucket()
    bucket.add(usage, request_cost or 0.0)
    buckets[day_key][display_model].merge(bucket)

    return ts, request_cost


def scan_project_litellm(  # noqa: C901
    path: Path,
    provider_tiered_prices: dict[str, dict | None],
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], bool]:
    """Scan LiteLLM logs for a project and aggregate token usage."""
    logs_dir = path / ".claude" / "litellm-logs"
    if not logs_dir.is_dir():
        return 0, None, {}, False

    buckets: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    last_ts: datetime | None = None
    session_count = 0

    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        provider_name = provider_dir.name
        tiered = provider_tiered_prices.get(provider_name)

        for session_dir in provider_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_count += 1
            messages_file = session_dir / "messages.jsonl"
            if not messages_file.is_file():
                continue

            try:
                with messages_file.open("r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        try:
                            rec = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue

                        ts, _ = _process_litellm_record(rec, provider_name, buckets, tiered)
                        if ts is not None and (last_ts is None or ts > last_ts):
                            last_ts = ts
            except OSError:
                continue

    return session_count, last_ts, {d: dict(m) for d, m in buckets.items()}, True


def load_projects(reg: Path) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for line in reg.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(Path(s))
    return out


def _build_total_body(
    totals_by_model: dict[str, Bucket],
    tree_root: Node,
    display_rows: list[DisplayRow],
    div: str,
) -> list:
    body: list = []

    if totals_by_model:
        ordered = sorted(
            totals_by_model.items(),
            key=lambda kv: kv[1].cost or 0.0,
            reverse=True,
        )
        for model, b in ordered:
            body.append(
                (
                    [
                        model,
                        "",
                        "",
                        fmt_count(b.msgs),
                        fmt_count(b.in_),
                        fmt_count(b.out),
                        fmt_count(b.cw),
                        fmt_count(b.cr),
                        fmt_cost(b.cost if b.cost > 0.0 else None),
                    ],
                    "",
                    0,
                )
            )
        body.append(div)

    body.append(
        (
            [
                "/",
                str(tree_root.subtree_sessions),
                fmt_ts(tree_root.subtree_last_ts),
                fmt_count(tree_root.subtree_bucket.msgs),
                fmt_count(tree_root.subtree_bucket.in_),
                fmt_count(tree_root.subtree_bucket.out),
                fmt_count(tree_root.subtree_bucket.cw),
                fmt_count(tree_root.subtree_bucket.cr),
                fmt_cost_with_unknown(
                    tree_root.subtree_known_cost, unknown=tree_root.subtree_unknown
                ),
            ],
            Ansi.DIM,
            0,
        )
    )
    for dr in display_rows:
        style = Ansi.DIM if dr.is_structural else ""
        body.append(
            (
                [
                    dr.label,
                    str(dr.sessions),
                    fmt_ts(dr.last_ts),
                    fmt_count(dr.bucket.msgs),
                    fmt_count(dr.bucket.in_),
                    fmt_count(dr.bucket.out),
                    fmt_count(dr.bucket.cw),
                    fmt_count(dr.bucket.cr),
                    dr.cost_str,
                ],
                style,
                dr.prefix_len,
            )
        )

    return body


def _aggregate_day_rows(
    dated: dict[str, dict[str, Bucket]],
    shown_days: list[str],
) -> tuple[list[tuple[str, Bucket, float, bool]], Bucket, float, bool]:
    day_rows_data: list[tuple[str, Bucket, float, bool]] = []
    for d in shown_days:
        day_total = Bucket()
        day_cost: float = 0.0
        day_unknown = False
        for b in dated[d].values():
            day_total.merge(b)
            if b.cost <= 0.0:
                day_unknown = True
            else:
                day_cost += b.cost
        day_rows_data.append((d, day_total, day_cost, day_unknown))

    total_b = Bucket()
    total_cost: float = 0.0
    total_unknown = False
    for _, b, c, unk in day_rows_data:
        total_b.merge(b)
        if unk:
            total_unknown = True
        else:
            total_cost += c

    return day_rows_data, total_b, total_cost, total_unknown


def _build_recent_body(
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    days_window: int,
    div: str,
) -> tuple[list, str]:
    body: list = []
    truncation_note = ""

    dated = {d: m for d, m in totals_by_day_by_model.items() if d != "?"}
    all_days_sorted = sorted(dated.keys(), reverse=True) if dated else []
    if dated and days_window > 0:
        cutoff = (datetime.now().astimezone().date() - timedelta(days=days_window - 1)).isoformat()
        shown_days = [d for d in all_days_sorted if d >= cutoff]
    else:
        shown_days = all_days_sorted

    recent_models: dict[str, Bucket] = defaultdict(Bucket)
    for d in shown_days:
        for model, b in dated[d].items():
            recent_models[model].merge(b)

    if recent_models:
        ordered = sorted(
            recent_models.items(),
            key=lambda kv: kv[1].cost or 0.0,
            reverse=True,
        )
        for model, b in ordered:
            body.append(
                (
                    [
                        model,
                        fmt_count(b.msgs),
                        fmt_count(b.in_),
                        fmt_count(b.out),
                        fmt_count(b.cw),
                        fmt_count(b.cr),
                        fmt_cost(b.cost if b.cost > 0.0 else None),
                    ],
                    "",
                    0,
                )
            )

    if shown_days:
        if body:
            body.append(div)

        day_rows_data, total_b, total_cost, total_unknown = _aggregate_day_rows(dated, shown_days)

        for d, b, day_cost, day_unknown in reversed(day_rows_data):
            cost_str = fmt_cost_with_unknown(day_cost, unknown=day_unknown)
            body.append(
                (
                    [
                        d,
                        fmt_count(b.msgs),
                        fmt_count(b.in_),
                        fmt_count(b.out),
                        fmt_count(b.cw),
                        fmt_count(b.cr),
                        cost_str,
                    ],
                    "",
                    0,
                )
            )

        body.append(div)
        body.append(
            (
                [
                    "TOTAL",
                    fmt_count(total_b.msgs),
                    fmt_count(total_b.in_),
                    fmt_count(total_b.out),
                    fmt_count(total_b.cw),
                    fmt_count(total_b.cr),
                    fmt_cost_with_unknown(total_cost, unknown=total_unknown),
                ],
                Ansi.BOLD_YELLOW,
                0,
            )
        )
        n_days = len(shown_days)
        body.append(
            (
                [
                    "DAILY AVG",
                    fmt_count(total_b.msgs // n_days),
                    fmt_count(total_b.in_ // n_days),
                    fmt_count(total_b.out // n_days),
                    fmt_count(total_b.cw // n_days),
                    fmt_count(total_b.cr // n_days),
                    fmt_cost_with_unknown(total_cost / n_days, unknown=total_unknown),
                ],
                Ansi.BOLD_YELLOW,
                0,
            )
        )

        if days_window > 0 and len(shown_days) < len(all_days_sorted):
            truncation_note = (
                f"  (showing last {len(shown_days)} of "
                f"{len(all_days_sorted)} days with activity; "
                f"use --days 0 to widen)"
            )

    return body, truncation_note


def _widths_for(
    headers: list[str], body: list, leading: int, shared_widths: list[int], div: str
) -> list[int]:
    leading_widths = [len(headers[j]) for j in range(leading)]
    for item in body:
        if item == div:
            continue
        cells, _, _ = item
        for j in range(leading):
            leading_widths[j] = max(leading_widths[j], len(cells[j]))
    return leading_widths + shared_widths


def _render_row(
    cells: list[str],
    aligns: list[str],
    widths: list[int],
    style: str = "",
    prefix_len: int = 0,
) -> str:
    parts = [f" {cell:{aligns[i]}{widths[i]}} " for i, cell in enumerate(cells)]
    if style:
        if prefix_len:
            first = parts[0]
            head = first[: 1 + prefix_len]
            tail = first[1 + prefix_len :]
            parts[0] = head + color(tail, style)
            parts[1:] = [color(p, style) for p in parts[1:]]
        else:
            parts = [color(p, style) for p in parts]
    sep = color("│", Ansi.DIM)
    return sep + sep.join(parts) + sep


def _make_border(widths: list[int], left: str, mid: str, right: str) -> str:
    parts = ["─" * (w + 2) for w in widths]
    return color(left + mid.join(parts) + right, Ansi.DIM)


def _render_table(  # noqa: PLR0913
    title: str,
    headers: list[str],
    aligns: list[str],
    body: list,
    leading: int,
    shared_widths: list[int],
    div: str,
) -> list[str]:
    widths = _widths_for(headers, body, leading, shared_widths, div)
    out = [color(title, Ansi.DIM)]
    out.append(_make_border(widths, "┌", "┬", "┐"))
    out.append(_render_row(headers, aligns, widths, Ansi.DIM))
    out.append(_make_border(widths, "├", "┼", "┤"))
    for item in body:
        if item == div:
            out.append(_make_border(widths, "├", "┼", "┤"))
        else:
            cells, style, prefix_len = item
            out.append(_render_row(cells, aligns, widths, style, prefix_len))
    out.append(_make_border(widths, "└", "┴", "┘"))
    return out


def _compute_shared_widths(
    tables: list[tuple[list[str], list, int]],
    n_shared: int,
    div: str,
) -> list[int]:
    shared_widths = [0] * n_shared
    for headers, body, leading in tables:
        for j in range(n_shared):
            shared_widths[j] = max(shared_widths[j], len(headers[leading + j]))
        for item in body:
            if item == div:
                continue
            cells, _, _ = item
            for j in range(n_shared):
                shared_widths[j] = max(shared_widths[j], len(cells[leading + j]))
    return shared_widths


def render(
    rows: list[dict],
    totals_by_model: dict[str, Bucket],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    days_window: int,
) -> str:
    shared_headers = ["MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    shared_aligns = [">", ">", ">", ">", ">", ">"]
    n_shared = len(shared_headers)

    div = "__div__"

    total_headers = ["MODEL / PROJECT", "SESSIONS", "LAST LAUNCH", *shared_headers]
    total_aligns = ["<", ">", "<", *shared_aligns]

    tree_root = build_project_tree(rows)
    display_rows = flatten_tree(tree_root)

    total_body = _build_total_body(totals_by_model, tree_root, display_rows, div)

    recent_headers = ["MODEL / DATE", *shared_headers]
    recent_aligns = ["<", *shared_aligns]

    recent_body, by_day_truncation_note = _build_recent_body(
        totals_by_day_by_model, days_window, div
    )

    shared_widths = _compute_shared_widths(
        [(total_headers, total_body, 3), (recent_headers, recent_body, 1)],
        n_shared,
        div,
    )

    lines: list[str] = []
    lines.extend(
        _render_table("Total:", total_headers, total_aligns, total_body, 3, shared_widths, div)
    )
    if recent_body:
        recent_title = "Recent:" if days_window == 0 else f"Recent (last {days_window} days):"
        lines.append("")
        lines.extend(
            _render_table(
                recent_title, recent_headers, recent_aligns, recent_body, 1, shared_widths, div
            )
        )
        if by_day_truncation_note:
            lines.append(color(by_day_truncation_note, Ansi.DIM))

    return "\n".join(lines)


_USAGE_TEXT = (
    "Usage: agent stats [--cache PATH] [--refresh] [--days N] <projects.txt>\n\n"
    "Reads a list of project paths (one per line) and prints aggregated\n"
    "usage stats from each project's .claude/litellm-logs/ directories.\n\n"
    "Output is a per-project table plus per-model and per-day breakdowns.\n"
    "Models are displayed as <provider>/<model>. Day buckets use host-local time.\n"
    "--days N limits the per-day section to the most recent N calendar days\n"
    "(default 30; use 0 to show all).\n\n"
    "Pricing is fetched dynamically per-provider as logs are scanned.\n\n"
    "Projects are recorded by `agent` on each launch — a project that\n"
    "has never had `agent` invoked from it will not appear here."
)


@dataclass
class _UsageArgsBuilder:
    cache_path: Path | None = None
    refresh: bool = False
    days_window: int = 30


@dataclass
class _UsageArgs:
    registry_path: Path
    cache_path: Path | None = None
    refresh: bool = False
    days_window: int = 30


def _parse_days(value: str) -> int | None:
    try:
        days = int(value)
    except ValueError:
        print(f"usage: --days expects an integer, got '{value}'", file=sys.stderr)
        return None
    if days < 0:
        print("usage: --days must be >= 0", file=sys.stderr)
        return None
    return days


def _parse_usage_args(args: list[str]) -> _UsageArgs | None:
    parsed = _UsageArgsBuilder()
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(_USAGE_TEXT, file=sys.stderr)
            return None
        if a == "--cache" and i + 1 < len(args):
            parsed.cache_path = Path(args[i + 1])
            i += 2
            continue
        if a == "--refresh":
            parsed.refresh = True
            i += 1
            continue
        if a == "--days" and i + 1 < len(args):
            days = _parse_days(args[i + 1])
            if days is None:
                return None
            parsed.days_window = days
            i += 2
            continue
        positional.append(a)
        i += 1

    if not positional:
        print(
            "Usage: agent stats [--cache PATH] [--refresh] [--days N] <projects.txt>",
            file=sys.stderr,
        )
        return None

    reg = Path(positional[0])
    if not reg.is_file():
        print(f"usage: registry not found at {reg}", file=sys.stderr)
        return None

    return _UsageArgs(
        registry_path=reg,
        **parsed.__dict__,
    )


def _get_provider_pricing(
    provider_name: str,
    provider_prices: dict[str, dict[str, dict[str, float]]],
    provider_tiered_prices: dict[str, dict | None],
) -> dict[str, dict[str, float]]:
    """Fetch and cache pricing for a provider."""
    if provider_name not in provider_prices:
        try:
            provider = get_provider(provider_name)
            provider_prices[provider_name] = provider.get_pricing()
            provider_tiered_prices[provider_name] = provider.get_tiered_pricing()
        except Exception:  # noqa: BLE001
            provider_prices[provider_name] = {}
            provider_tiered_prices[provider_name] = None
    return provider_prices[provider_name]


def _collect_project_rows_with_pricing(
    projects: list[Path],
) -> tuple[
    list[dict],
    dict[str, Bucket],
    dict[str, dict[str, Bucket]],
    dict[str, dict[str, dict[str, float]]],
]:
    rows: list[dict] = []
    totals_by_model: dict[str, Bucket] = defaultdict(Bucket)
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    provider_prices: dict[str, dict[str, dict[str, float]]] = {}
    provider_tiered_prices: dict[str, dict | None] = {}

    for path in projects:
        logs_dir = path / ".claude" / "litellm-logs"
        if not logs_dir.is_dir():
            continue

        for provider_dir in logs_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            _get_provider_pricing(provider_dir.name, provider_prices, provider_tiered_prices)

        sessions, last_ts, by_day, exists = scan_project_litellm(path, provider_tiered_prices)
        total = Bucket()
        for day, by_model in by_day.items():
            for model, b in by_model.items():
                total.merge(b)
                totals_by_model[model].merge(b)
                totals_by_day_by_model[day][model].merge(b)

        # total.cost now contains the sum of per-request costs calculated during scan
        proj_cost = total.cost if total.cost > 0.0 else None

        if sessions > 0 or exists:
            rows.append(
                {
                    "path": path,
                    "exists": exists,
                    "sessions": sessions,
                    "last_ts": last_ts,
                    "total": total,
                    "cost": proj_cost,
                }
            )

    rows.sort(key=lambda r: r["cost"] if r["cost"] is not None else -1.0, reverse=True)
    return (
        rows,
        dict(totals_by_model),
        {d: dict(m) for d, m in totals_by_day_by_model.items()},
        provider_prices,
    )


def run(args: list[str], tool_dir: Path) -> int:
    projects_file = tool_dir / ".agent-launches" / "projects.txt"

    # Inject tool-dir-derived paths into the args stream
    injected = [str(projects_file), *args]
    parsed = _parse_usage_args(injected)
    if parsed is None:
        return 1 if args and args[0] not in ("-h", "--help") else 0

    projects = load_projects(parsed.registry_path)
    if not projects:
        print(
            "usage: no projects recorded yet — launch `agent` once to register a project.",
            file=sys.stderr,
        )
        return 0

    rows, totals_by_model, totals_by_day_by_model, _provider_prices = (
        _collect_project_rows_with_pricing(projects)
    )

    # Filter out projects with no logs
    rows = [r for r in rows if r["sessions"] > 0]
    if not rows:
        print("usage: no LiteLLM logs found for any registered project.", file=sys.stderr)
        return 0

    print(
        render(
            rows,
            totals_by_model,
            totals_by_day_by_model,
            parsed.days_window,
        )
    )
    return 0
