# This file has been edited with the assistance of an AI tool.
"""The `legacy_stats` subcommand — aggregate Claude Code usage stats from session data."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent_wrap.lib.console import Ansi
from agent_wrap.providers import get_provider

USAGE = "[--cache PATH] [--refresh] [--days N]"
SUMMARY = "Show token usage stats (reads from .claude/sessions/*.jsonl)"

_BILLION = 1_000_000_000
_MILLION = 1_000_000
_THOUSAND = 1_000


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


_MODEL_FAMILY_RE_V_FIRST = re.compile(
    r"claude[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)[-\s.]*(?P<tier>opus|sonnet|haiku)",
    re.IGNORECASE,
)
_MODEL_FAMILY_RE_T_FIRST = re.compile(
    r"claude[-\s.]*(?P<tier>opus|sonnet|haiku)[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


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


def cost_for(model: str, usage: dict, prices: dict) -> float | None:
    # Synthetic / placeholder records contribute no tokens; treat them as
    # zero-cost rather than poisoning the project's total with "?".
    if not any(usage.values()):
        return 0.0
    key = normalize_model(model)
    if key is None:
        return None
    p = prices.get(key)
    if p is None:
        return None
    return (
        usage["in"] * p["in"] / 1_000_000
        + usage["out"] * p["out"] / 1_000_000
        + usage["cw_5m"] * p["cw_5m"] / 1_000_000
        + usage["cw_1h"] * p["cw_1h"] / 1_000_000
        + usage["cr"] * p["cr"] / 1_000_000
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
    __slots__ = ("cr", "cw_1h", "cw_5m", "in_", "msgs", "out")

    def __init__(self) -> None:
        self.msgs = 0
        self.in_ = 0
        self.out = 0
        self.cw_5m = 0
        self.cw_1h = 0
        self.cr = 0

    def add(self, usage: dict) -> None:
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
            # Older sessions / non-Anthropic providers may only set the flat
            # `cache_creation_input_tokens` total. Charge those at the 5m rate.
            self.cw_5m += usage.get("cache_creation_input_tokens", 0) or 0
        self.cr += usage.get("cache_read_input_tokens", 0) or 0

    def merge(self, other: Bucket) -> None:
        self.msgs += other.msgs
        self.in_ += other.in_
        self.out += other.out
        self.cw_5m += other.cw_5m
        self.cw_1h += other.cw_1h
        self.cr += other.cr

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
    """
    One node in the path trie used to render the per-project tree.

    A node is either *structural* (`row is None`, e.g. `/`, `home/`, an
    intermediate path segment) or a *project* node carrying the row dict
    produced by `scan_project`. `subtree_*` fields are aggregates over the
    node and all its descendants, populated by `_aggregate` after the trie
    has been compressed and self-rows split.
    """

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
    """
    Build a path trie over `rows`, then compress single-child structural
    chains and split projects-with-children into a `.` self-row.
    """
    root = Node("/")
    for r in rows:
        parts = Path(r["path"]).parts
        if not parts:
            continue
        # On absolute paths Path.parts starts with "/"; we use the synthetic
        # root for that. Relative paths (shouldn't appear in projects.txt
        # but handled defensively) are placed under root as well.
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
    """
    Fold `parent/child` into one node when the parent is structural and
    has exactly one child. The synthetic root is exempt (it stays as `/`).
    """
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
    """
    For project nodes that also have children (e.g. `mm-builder` with
    `mm-builder/mm_random` underneath), move the project's own row to a
    synthetic `.` child so the parent can render as a structural subtotal.
    """
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
    """Post-order: fill `subtree_*` fields on every node."""
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
    """
    Walk the tree in display order, producing one DisplayRow per visible
    line. The root itself is not emitted; callers prepend their own banner.
    """
    out: list[DisplayRow] = []

    def walk(node: Node, ancestors_continue: list[bool]) -> None:
        children = list(node.children.values())
        # `.` is pinned first (it represents the parent directory's own
        # project row, so it visually belongs immediately under the parent).
        # Then leaves (no children of their own — single-project rows)
        # alphabetically, then subtree nodes ordered by ascending project
        # count so the bushiest groups sink to the bottom.
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


def _process_record(
    rec: dict,
    seen_message_ids: set[str],
    buckets: dict[str, dict[str, Bucket]],
) -> datetime | None:
    """Process a single JSONL record into buckets. Returns the timestamp if valid."""
    ts = parse_ts(rec.get("timestamp"))
    if rec.get("type") != "assistant":
        return ts
    msg = rec.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return ts
    msg_id = msg.get("id")
    if msg_id:
        if msg_id in seen_message_ids:
            return ts
        seen_message_ids.add(msg_id)
    model = msg.get("model") or "?"
    day_key = ts.astimezone().strftime("%Y-%m-%d") if ts is not None else "?"
    buckets[day_key][model].add(usage)
    return ts


def scan_project(
    path: Path,
    seen_message_ids: set[str],
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], bool]:
    """
    Return (session_count, last_ts, per_day_per_model_buckets, exists).

    The buckets dict is keyed `day → model → Bucket`, where `day` is either a
    `YYYY-MM-DD` string in host-local time or `"?"` for records whose
    timestamp was missing or unparseable. `exists` is False when the project
    directory or its sessions dir is gone.

    `seen_message_ids` is a global set used to dedupe assistant records whose
    `message.id` has already been counted. Claude Code replays prior turns
    into new session files when a session is resumed/compacted/forked, so the
    same `msg_bdrk_…` id appears in multiple `.jsonl` files; Bedrock bills the
    underlying call once. The set is mutated in place across all projects.
    """
    sessions_dir = path / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return 0, None, {}, False

    # Top-level *.jsonl is one file per resumable conversation; subagent
    # invocations live in `<sid>/subagents/*.jsonl` under each session and
    # carry their own billable assistant turns. We scan both, but the
    # session-count column reflects only top-level files (the user-facing
    # "session" granularity).
    top_files = sorted(sessions_dir.glob("*.jsonl"))
    files = sorted(sessions_dir.rglob("*.jsonl"))
    last_ts: datetime | None = None
    buckets: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _process_record(rec, seen_message_ids, buckets)
                    if ts is not None and (last_ts is None or ts > last_ts):
                        last_ts = ts
        except OSError:
            continue

    return len(top_files), last_ts, {d: dict(m) for d, m in buckets.items()}, True


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
    prices: dict,
    tree_root: Node,
    display_rows: list[DisplayRow],
    div: str,
) -> list:
    """Build the body rows for the Total table."""
    body: list = []

    if totals_by_model:
        ordered = sorted(
            totals_by_model.items(),
            key=lambda kv: cost_for(kv[0], kv[1].usage_dict(), prices) or 0.0,
            reverse=True,
        )
        for model, b in ordered:
            c = cost_for(model, b.usage_dict(), prices)
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
                        fmt_cost(c),
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
    prices: dict,
) -> tuple[list[tuple[str, Bucket, float, bool]], Bucket, float, bool]:
    """Aggregate per-day rows. Returns (day_rows_data, total_bucket, total_cost, total_unknown)."""
    day_rows_data: list[tuple[str, Bucket, float, bool]] = []
    for d in shown_days:
        day_total = Bucket()
        day_cost: float = 0.0
        day_unknown = False
        for model, b in dated[d].items():
            day_total.merge(b)
            c = cost_for(model, b.usage_dict(), prices)
            if c is None:
                day_unknown = True
            else:
                day_cost += c
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
    prices: dict,
    days_window: int,
    div: str,
) -> tuple[list, str]:
    """Build the body rows for the Recent table. Returns (body, truncation_note)."""
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
            key=lambda kv: cost_for(kv[0], kv[1].usage_dict(), prices) or 0.0,
            reverse=True,
        )
        for model, b in ordered:
            c = cost_for(model, b.usage_dict(), prices)
            body.append(
                (
                    [
                        model,
                        fmt_count(b.msgs),
                        fmt_count(b.in_),
                        fmt_count(b.out),
                        fmt_count(b.cw),
                        fmt_count(b.cr),
                        fmt_cost(c),
                    ],
                    "",
                    0,
                )
            )

    if shown_days:
        if body:
            body.append(div)

        day_rows_data, total_b, total_cost, total_unknown = _aggregate_day_rows(
            dated, shown_days, prices
        )

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
    """Compute column widths for a table."""
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
    """Render a single table row with alignment and optional styling."""
    parts = [f" {cell:{aligns[i]}{widths[i]}} " for i, cell in enumerate(cells)]
    if style:
        if prefix_len:
            # Keep tree glyphs (`├`, `└`, `│`) at the row's default color;
            # only style the content after the prefix.
            first = parts[0]
            # the cell starts after the leading space
            head = first[: 1 + prefix_len]
            tail = first[1 + prefix_len :]
            parts[0] = head + color(tail, style)
            parts[1:] = [color(p, style) for p in parts[1:]]
        else:
            parts = [color(p, style) for p in parts]
    sep = color("│", Ansi.DIM)
    return sep + sep.join(parts) + sep


def _make_border(widths: list[int], left: str, mid: str, right: str) -> str:
    """Render a horizontal border line."""
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
    """Render a complete table with borders."""
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
    """Compute shared column widths across multiple tables."""
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
    prices: dict,
    days_window: int,
) -> str:
    # Two stacked tables: "Total" (all-time per-model + per-project tree) and
    # "Recent" (per-model + per-day, both restricted to the days_window). Each
    # table has internal sections separated by a `├─┼─┤` divider; widths of
    # the trailing six numeric columns are shared across both tables so the
    # numbers line up vertically.
    shared_headers = ["MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    shared_aligns = [">", ">", ">", ">", ">", ">"]
    n_shared = len(shared_headers)

    div = "__div__"

    # === Total table: models (all-time) + project tree ===
    total_headers = ["MODEL / PROJECT", "SESSIONS", "LAST LAUNCH", *shared_headers]
    total_aligns = ["<", ">", "<", *shared_aligns]

    tree_root = build_project_tree(rows)
    display_rows = flatten_tree(tree_root)

    total_body = _build_total_body(totals_by_model, prices, tree_root, display_rows, div)

    # === Recent table: models (in window) + per-day (in window) + TOTAL ===
    recent_headers = ["MODEL / DATE", *shared_headers]
    recent_aligns = ["<", *shared_aligns]

    recent_body, by_day_truncation_note = _build_recent_body(
        totals_by_day_by_model, prices, days_window, div
    )

    # === Shared widths for the trailing six numeric columns ===
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
    "Usage: agent legacy_stats [--cache PATH] [--refresh] [--days N] <projects.txt>\n\n"
    "Reads a list of project paths (one per line) and prints aggregated\n"
    "usage stats from each project's .claude/sessions/*.jsonl files.\n\n"
    "Output is a per-project table plus per-model and per-day breakdowns.\n"
    "Day buckets use host-local time. --days N limits the per-day section\n"
    "to the most recent N calendar days (default 30; use 0 to show all).\n\n"
    "Pricing is fetched from the default provider (litellm-bedrock).\n\n"
    "Projects are recorded by `agent` on each launch — a project that\n"
    "has never had `agent` invoked from it will not appear here."
)


@dataclass
class _UsageArgsBuilder:
    """Parsed CLI arguments for `agent legacy_stats`."""

    cache_path: Path | None = None
    refresh: bool = False
    days_window: int = 30


@dataclass
class _UsageArgs:
    """Parsed CLI arguments for `agent legacy_stats`."""

    registry_path: Path
    cache_path: Path | None = None
    refresh: bool = False
    days_window: int = 30


def _parse_days(value: str) -> int | None:
    """Parse --days value. Returns None on error."""
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
    """Parse CLI arguments. Returns None if help was printed or an error occurred."""
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
            "Usage: agent legacy_stats [--cache PATH] [--refresh] [--days N] <projects.txt>",
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


def _collect_project_rows(
    projects: list[Path],
    prices: dict,
) -> tuple[list[dict], dict[str, Bucket], dict[str, dict[str, Bucket]]]:
    """Scan all projects and return (rows, totals_by_model, totals_by_day_by_model)."""
    rows: list[dict] = []
    totals_by_model: dict[str, Bucket] = defaultdict(Bucket)
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    seen_message_ids: set[str] = set()

    for path in projects:
        sessions, last_ts, by_day, exists = scan_project(path, seen_message_ids)
        total = Bucket()
        proj_cost: float = 0.0
        proj_unknown = False
        for day, by_model in by_day.items():
            for model, b in by_model.items():
                total.merge(b)
                totals_by_model[model].merge(b)
                totals_by_day_by_model[day][model].merge(b)
                c = cost_for(model, b.usage_dict(), prices)
                if c is None:
                    proj_unknown = True
                else:
                    proj_cost += c
        rows.append(
            {
                "path": path,
                "exists": exists,
                "sessions": sessions,
                "last_ts": last_ts,
                "total": total,
                "cost": None if proj_unknown else proj_cost,
            }
        )

    rows.sort(key=lambda r: r["cost"] if r["cost"] is not None else -1.0, reverse=True)
    return rows, dict(totals_by_model), {d: dict(m) for d, m in totals_by_day_by_model.items()}


def run(args: list[str], tool_dir: Path) -> int:
    """
    Execute the `legacy_stats` subcommand.

    Constructs the cache and project-registry paths from ``tool_dir``,
    injects them into the argument stream, and runs the core usage logic.
    """
    cache_path = tool_dir / ".agent-launches" / "pricing.json"
    projects_file = tool_dir / ".agent-launches" / "projects.txt"

    # Inject tool-dir-derived paths into the args stream so _parse_usage_args
    # can find --cache and the positional registry path.
    injected = ["--cache", str(cache_path), str(projects_file), *args]
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

    # Fetch pricing from the default provider
    try:
        provider = get_provider("litellm-bedrock")
        prices = provider.get_pricing()
    except Exception:  # noqa: BLE001
        prices = {}

    rows, totals_by_model, totals_by_day_by_model = _collect_project_rows(projects, prices)

    print(
        render(
            rows,
            totals_by_model,
            totals_by_day_by_model,
            prices,
            parsed.days_window,
        )
    )
    return 0
