#!/usr/bin/env python3
# This file has been created with the assistance of an AI tool.
"""Aggregate Claude Code usage stats across all projects where `agent` was launched.

Reads a list of project paths (one per line) and walks each project's
`.claude/sessions/*.jsonl` files, summing token usage and estimated cost.
Prints an aligned text table sorted by cost descending, plus a per-model
breakdown.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

# Pricing source: the AWS Bedrock pricing page renders per-model cells as
# `{priceOf!bedrockfoundationmodels/bedrockfoundationmodels!<KEY>}` placeholders;
# at runtime the real numbers are pulled from `bedrockfoundationmodels.json`
# keyed by region. We do the same join offline and cache the resolved table.
PRICING_PAGE_URL = "https://aws.amazon.com/bedrock/pricing/"
PRICING_DATA_URL = (
    "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/"
    "bedrockfoundationmodels/USD/current/bedrockfoundationmodels.json"
)
PRICING_CACHE_TTL_SECONDS = 7 * 24 * 3600
PRICING_FETCH_TIMEOUT = 15
DEFAULT_REGION_LABEL = "US East (N. Virginia)"

# Two known column schemas on the AWS Bedrock pricing page, picked by key
# count per row. The newest models (e.g. Opus 4.7) drop the batch columns.
PRICING_SCHEMAS = {
    7: ("in", "out", "in_batch", "out_batch", "cw_5m", "cw_1h", "cr"),
    5: ("in", "out", "cw_5m", "cw_1h", "cr"),
}

DIM = "\033[90m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"


def color(s: str, code: str) -> str:
    return f"{code}{s}{RESET}" if sys.stdout.isatty() else s


def fmt_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}G"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(c: float | None) -> str:
    if c is None:
        return "?"
    return f"${c:.2f}"


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "agent-wrap/agent_usage",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=PRICING_FETCH_TIMEOUT) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def _scrape_model_keys(page_html: str) -> dict[str, tuple[tuple[str, ...], list[str]]]:
    """Extract `{normalized_model: (column_schema, priceOf_keys)}` from the
    Bedrock pricing page HTML.

    Anthropic Claude rows publish either 5 or 7 priceOf columns depending on
    model age (newer models like Opus 4.7 dropped the batch columns); we
    accept both schemas and let the caller resolve them.

    Each model is published in two sections — "Global Cross-region Inference"
    (cheaper) and "Geo and In-region Cross-region Inference" (~10% premium).
    Claude Code on Bedrock invokes via `us.anthropic.claude-*` inference
    profiles, which bill at the geo-tier rate. We prefer the geo row when
    both are present; if only one is present, we use it.
    """
    src = html.unescape(page_html).replace("\\u003c", "<").replace("\\u003e", ">")
    src = src.replace('\\"', '"')

    row_re = re.compile(r"<tr[^>]*>(?P<row>.*?</tr>)", re.DOTALL)
    name_re = re.compile(r"Claude\s+(Opus|Sonnet|Haiku)\s+(\d+(?:\.\d+)*)")
    key_re = re.compile(
        r"priceOf!bedrockfoundationmodels/bedrockfoundationmodels!"
        r"([A-Za-z0-9_-]+)"
    )
    # Section markers appear as <h2>...</h2> at the start of each pricing
    # table's markup blob. We scan rows in document order and remember the
    # most recent section heading we passed.
    section_re = re.compile(
        r"<h2[^>]*>\s*(Global Cross-region Inference"
        r"|Geo and In-region Cross-region Inference)\s*</h2>",
        re.IGNORECASE,
    )
    section_starts: list[tuple[int, str]] = [
        (m.start(), "geo" if m.group(1).lower().startswith("geo") else "global")
        for m in section_re.finditer(src)
    ]

    def section_at(pos: int) -> str:
        cur = "global"
        for start, name in section_starts:
            if start <= pos:
                cur = name
            else:
                break
        return cur

    # tier-rank: geo wins over global (Claude Code uses us.* inference profiles)
    rank = {"geo": 1, "global": 0}
    out: dict[str, tuple[int, tuple[str, ...], list[str]]] = {}
    for m in row_re.finditer(src):
        row = m.group("row")
        nm = name_re.search(row)
        if not nm:
            continue
        keys = key_re.findall(row)
        schema = PRICING_SCHEMAS.get(len(keys))
        if schema is None:
            continue
        tier_name = section_at(m.start())
        tier_rank = rank[tier_name]
        canonical = f"claude-{nm.group(1).lower()}-{nm.group(2).replace('.', '-')}"
        prev = out.get(canonical)
        if prev is None or tier_rank > prev[0]:
            out[canonical] = (tier_rank, schema, keys)
    return {k: (s, ks) for k, (_, s, ks) in out.items()}


def _build_pricing_table(
    page_html: str, data_json: dict, region_label: str
) -> dict[str, dict[str, float]]:
    """Join the page's model→keys map with the data file's region→key→price
    map, producing `{canonical_model: {in,out,cw_5m,cw_1h,cr: $/MTok}}`.

    Both cache-write rates are kept because session usage records split the
    counts via `usage.cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens`
    and the 1h rate is materially higher (e.g. Opus 4.7: 5m=$6.25, 1h=$10).
    """
    region = data_json.get("regions", {}).get(region_label) or {}
    keys_by_model = _scrape_model_keys(page_html)

    table: dict[str, dict[str, float]] = {}
    for canonical, (schema, keys) in keys_by_model.items():
        cols = dict(zip(schema, keys))
        try:
            row = {
                "in":    float(region[cols["in"]]["price"]),
                "out":   float(region[cols["out"]]["price"]),
                "cw_5m": float(region[cols["cw_5m"]]["price"]),
                "cw_1h": float(region[cols["cw_1h"]]["price"]),
                "cr":    float(region[cols["cr"]]["price"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        table[canonical] = row
    return table


def load_prices(
    cache_path: Path | None,
    region_label: str = DEFAULT_REGION_LABEL,
    refresh: bool = False,
) -> dict[str, dict[str, float]]:
    """Return the pricing table, refreshing from AWS if cache is missing/stale.

    On any network or parse failure, falls back to the cached copy if there
    is one; otherwise returns an empty dict (callers will then render unknown
    costs as `?`).
    """
    cached: dict | None = None
    if cache_path is not None and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None

    fresh_enough = (
        cached is not None
        and cached.get("region") == region_label
        and isinstance(cached.get("fetched_at"), (int, float))
        and (time.time() - cached["fetched_at"]) < PRICING_CACHE_TTL_SECONDS
    )

    if fresh_enough and not refresh:
        return cached.get("prices") or {}

    try:
        page = _http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")
        data = json.loads(_http_get(PRICING_DATA_URL))
        prices = _build_pricing_table(page, data, region_label)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        if cached:
            return cached.get("prices") or {}
        sys.stderr.write(f"agent_usage: could not fetch pricing ({e}); costs will show as '?'.\n")
        return {}

    if not prices:
        if cached:
            return cached.get("prices") or {}
        return {}

    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"region": region_label, "fetched_at": time.time(), "prices": prices},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    return prices


_MODEL_FAMILY_RE_V_FIRST = re.compile(
    r"claude[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)[-\s.]*(?P<tier>opus|sonnet|haiku)",
    re.IGNORECASE,
)
_MODEL_FAMILY_RE_T_FIRST = re.compile(
    r"claude[-\s.]*(?P<tier>opus|sonnet|haiku)[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)",
    re.IGNORECASE,
)


def normalize_model(model: str) -> str | None:
    """Return a canonical 'claude-<tier>-<ver>' key for a session model id.

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
        usage["in"]    * p["in"]    / 1_000_000
        + usage["out"]   * p["out"]   / 1_000_000
        + usage["cw_5m"] * p["cw_5m"] / 1_000_000
        + usage["cw_1h"] * p["cw_1h"] / 1_000_000
        + usage["cr"]    * p["cr"]    / 1_000_000
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
    __slots__ = ("msgs", "in_", "out", "cw_5m", "cw_1h", "cr")

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

    def merge(self, other: "Bucket") -> None:
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
        return {"in": self.in_, "out": self.out,
                "cw_5m": self.cw_5m, "cw_1h": self.cw_1h, "cr": self.cr}


class Node:
    """One node in the path trie used to render the per-project tree.

    A node is either *structural* (`row is None`, e.g. `/`, `home/`, an
    intermediate path segment) or a *project* node carrying the row dict
    produced by `scan_project`. `subtree_*` fields are aggregates over the
    node and all its descendants, populated by `_aggregate` after the trie
    has been compressed and self-rows split.
    """
    __slots__ = (
        "name", "children", "row",
        "subtree_bucket", "subtree_known_cost", "subtree_unknown",
        "subtree_sessions", "subtree_last_ts", "subtree_project_count",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, "Node"] = {}
        self.row: dict | None = None
        self.subtree_bucket = Bucket()
        self.subtree_known_cost = 0.0
        self.subtree_unknown = False
        self.subtree_sessions = 0
        self.subtree_last_ts: datetime | None = None
        self.subtree_project_count = 0


class DisplayRow:
    __slots__ = ("label", "prefix_len", "is_structural", "sessions", "bucket", "last_ts", "cost_str")

    def __init__(
        self,
        label: str,
        prefix_len: int,
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
    """Build a path trie over `rows`, then compress single-child structural
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
    """Fold `parent/child` into one node when the parent is structural and
    has exactly one child. The synthetic root is exempt (it stays as `/`).
    """
    new_children: dict[str, Node] = {}
    for child in list(node.children.values()):
        _compress(child)
        while child.row is None and len(child.children) == 1:
            (gc,) = child.children.values()
            gc.name = f"{child.name}/{gc.name}"
            child = gc
        new_children[child.name] = child
    node.children = new_children


def _split_self_rows(node: Node) -> None:
    """For project nodes that also have children (e.g. `mm-builder` with
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
            node.subtree_last_ts is None
            or child.subtree_last_ts > node.subtree_last_ts
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
            node.subtree_last_ts is None
            or r["last_ts"] > node.subtree_last_ts
        ):
            node.subtree_last_ts = r["last_ts"]


def flatten_tree(root: Node) -> list[DisplayRow]:
    """Walk the tree in display order, producing one DisplayRow per visible
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
            is_last = (i == len(ordered) - 1)
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
                out.append(DisplayRow(
                    label=label,
                    prefix_len=prefix_len,
                    is_structural=False,
                    sessions=r["sessions"],
                    bucket=r["total"],
                    last_ts=r["last_ts"],
                    cost_str=cost_str,
                ))
            else:
                cost_str = fmt_cost(child.subtree_known_cost) + (
                    "+?" if child.subtree_unknown else ""
                )
                out.append(DisplayRow(
                    label=label,
                    prefix_len=prefix_len,
                    is_structural=True,
                    sessions=child.subtree_sessions,
                    bucket=child.subtree_bucket,
                    last_ts=child.subtree_last_ts,
                    cost_str=cost_str,
                ))

            if child.children:
                walk(child, ancestors_continue + [not is_last])

    walk(root, [])
    return out


def scan_project(
    path: Path,
    seen_message_ids: set[str],
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], bool]:
    """Return (session_count, last_ts, per_day_per_model_buckets, exists).

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
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(rec.get("timestamp"))
                    if ts is not None and (last_ts is None or ts > last_ts):
                        last_ts = ts
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    msg_id = msg.get("id")
                    if msg_id:
                        if msg_id in seen_message_ids:
                            continue
                        seen_message_ids.add(msg_id)
                    model = msg.get("model") or "?"
                    day_key = ts.astimezone().strftime("%Y-%m-%d") if ts is not None else "?"
                    buckets[day_key][model].add(usage)
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
    SHARED_HEADERS = ["MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    SHARED_ALIGNS = [">", ">", ">", ">", ">", ">"]
    N_SHARED = len(SHARED_HEADERS)

    DIV = "__div__"

    # === Total table: models (all-time) + project tree ===
    total_headers = ["MODEL / PROJECT", "SESSIONS", "LAST LAUNCH", *SHARED_HEADERS]
    total_aligns = ["<", ">", "<", *SHARED_ALIGNS]
    total_body: list = []

    if totals_by_model:
        ordered = sorted(
            totals_by_model.items(),
            key=lambda kv: (cost_for(kv[0], kv[1].usage_dict(), prices) or 0.0),
            reverse=True,
        )
        for model, b in ordered:
            c = cost_for(model, b.usage_dict(), prices)
            total_body.append(([
                model, "", "",
                fmt_count(b.msgs),
                fmt_count(b.in_),
                fmt_count(b.out),
                fmt_count(b.cw),
                fmt_count(b.cr),
                fmt_cost(c),
            ], "", 0))
        total_body.append(DIV)

    tree_root = build_project_tree(rows)
    display_rows = flatten_tree(tree_root)

    # The synthetic root row ("/") carries the grand-total subtree aggregates,
    # so it doubles as the project-section totals line.
    total_body.append(([
        "/",
        str(tree_root.subtree_sessions),
        fmt_ts(tree_root.subtree_last_ts),
        fmt_count(tree_root.subtree_bucket.msgs),
        fmt_count(tree_root.subtree_bucket.in_),
        fmt_count(tree_root.subtree_bucket.out),
        fmt_count(tree_root.subtree_bucket.cw),
        fmt_count(tree_root.subtree_bucket.cr),
        fmt_cost(tree_root.subtree_known_cost) + (
            "+?" if tree_root.subtree_unknown else ""
        ),
    ], DIM, 0))
    for dr in display_rows:
        style = DIM if dr.is_structural else ""
        total_body.append(([
            dr.label,
            str(dr.sessions),
            fmt_ts(dr.last_ts),
            fmt_count(dr.bucket.msgs),
            fmt_count(dr.bucket.in_),
            fmt_count(dr.bucket.out),
            fmt_count(dr.bucket.cw),
            fmt_count(dr.bucket.cr),
            dr.cost_str,
        ], style, dr.prefix_len))

    # === Recent table: models (in window) + per-day (in window) + TOTAL ===
    recent_headers = ["MODEL / DATE", *SHARED_HEADERS]
    recent_aligns = ["<", *SHARED_ALIGNS]
    recent_body: list = []

    dated = {d: m for d, m in totals_by_day_by_model.items() if d != "?"}
    all_days_sorted = sorted(dated.keys(), reverse=True) if dated else []
    if dated and days_window > 0:
        cutoff = (datetime.now().astimezone().date()
                  - timedelta(days=days_window - 1)).isoformat()
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
            key=lambda kv: (cost_for(kv[0], kv[1].usage_dict(), prices) or 0.0),
            reverse=True,
        )
        for model, b in ordered:
            c = cost_for(model, b.usage_dict(), prices)
            recent_body.append(([
                model,
                fmt_count(b.msgs),
                fmt_count(b.in_),
                fmt_count(b.out),
                fmt_count(b.cw),
                fmt_count(b.cr),
                fmt_cost(c),
            ], "", 0))

    by_day_truncation_note = ""
    if shown_days:
        if recent_body:
            recent_body.append(DIV)

        day_rows_data: list[tuple[str, Bucket, float | None]] = []
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
            day_rows_data.append((d, day_total, None if day_unknown else day_cost))

        total_b = Bucket()
        total_cost: float = 0.0
        total_unknown = False
        for _, b, c in day_rows_data:
            total_b.merge(b)
            if c is None:
                total_unknown = True
            else:
                total_cost += c

        for d, b, c in reversed(day_rows_data):
            cost_str = (fmt_cost(c) if c is not None
                        else fmt_cost(sum(  # show known portion + ?
                            cost_for(m, bb.usage_dict(), prices) or 0.0
                            for m, bb in dated[d].items()
                        )) + "+?")
            recent_body.append(([
                d,
                fmt_count(b.msgs),
                fmt_count(b.in_),
                fmt_count(b.out),
                fmt_count(b.cw),
                fmt_count(b.cr),
                cost_str,
            ], "", 0))

        recent_body.append(DIV)
        recent_body.append(([
            "TOTAL",
            fmt_count(total_b.msgs),
            fmt_count(total_b.in_),
            fmt_count(total_b.out),
            fmt_count(total_b.cw),
            fmt_count(total_b.cr),
            fmt_cost(total_cost) + ("+?" if total_unknown else ""),
        ], YELLOW, 0))
        n_days = len(shown_days)
        recent_body.append(([
            "DAILY AVG",
            fmt_count(total_b.msgs // n_days),
            fmt_count(total_b.in_ // n_days),
            fmt_count(total_b.out // n_days),
            fmt_count(total_b.cw // n_days),
            fmt_count(total_b.cr // n_days),
            fmt_cost(total_cost / n_days) + ("+?" if total_unknown else ""),
        ], YELLOW, 0))

        if days_window > 0 and len(shown_days) < len(all_days_sorted):
            by_day_truncation_note = (
                f"  (showing last {len(shown_days)} of "
                f"{len(all_days_sorted)} days with activity; "
                f"use --days 0 to widen)"
            )

    # === Shared widths for the trailing six numeric columns ===
    shared_widths = [0] * N_SHARED
    for headers, body, leading in (
        (total_headers, total_body, 3),
        (recent_headers, recent_body, 1),
    ):
        for j in range(N_SHARED):
            shared_widths[j] = max(shared_widths[j], len(headers[leading + j]))
        for item in body:
            if item == DIV:
                continue
            cells, _, _ = item
            for j in range(N_SHARED):
                shared_widths[j] = max(shared_widths[j], len(cells[leading + j]))

    def widths_for(headers: list[str], body: list, leading: int) -> list[int]:
        leading_widths = [len(headers[j]) for j in range(leading)]
        for item in body:
            if item == DIV:
                continue
            cells, _, _ = item
            for j in range(leading):
                if len(cells[j]) > leading_widths[j]:
                    leading_widths[j] = len(cells[j])
        return leading_widths + shared_widths

    def render_row(
        cells: list[str],
        aligns: list[str],
        widths: list[int],
        style: str = "",
        prefix_len: int = 0,
    ) -> str:
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
        sep = color("│", DIM)
        return sep + sep.join(parts) + sep

    def make_border(widths: list[int], left: str, mid: str, right: str) -> str:
        parts = ["─" * (w + 2) for w in widths]
        return color(left + mid.join(parts) + right, DIM)

    def render_table(
        title: str,
        headers: list[str],
        aligns: list[str],
        body: list,
        leading: int,
    ) -> list[str]:
        widths = widths_for(headers, body, leading)
        out = [color(title, DIM)]
        out.append(make_border(widths, "┌", "┬", "┐"))
        out.append(render_row(headers, aligns, widths, DIM))
        out.append(make_border(widths, "├", "┼", "┤"))
        for item in body:
            if item == DIV:
                out.append(make_border(widths, "├", "┼", "┤"))
            else:
                cells, style, prefix_len = item
                out.append(render_row(cells, aligns, widths, style, prefix_len))
        out.append(make_border(widths, "└", "┴", "┘"))
        return out

    lines: list[str] = []
    lines.extend(render_table("Total:", total_headers, total_aligns, total_body, 3))
    if recent_body:
        recent_title = (
            "Recent:" if days_window == 0
            else f"Recent (last {days_window} days):"
        )
        lines.append("")
        lines.extend(render_table(
            recent_title, recent_headers, recent_aligns, recent_body, 1
        ))
        if by_day_truncation_note:
            lines.append(color(by_day_truncation_note, DIM))

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = argv[1:]
    cache_path: Path | None = None
    region_label = DEFAULT_REGION_LABEL
    refresh = False
    days_window = 30
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(
                "Usage: agent_usage.py [--cache PATH] [--region LABEL] [--refresh] [--days N] <projects.txt>\n\n"
                "Reads a list of project paths (one per line) and prints aggregated\n"
                "usage stats from each project's .claude/sessions/*.jsonl files.\n\n"
                "Output is a per-project table plus per-model and per-day breakdowns.\n"
                "Day buckets use host-local time. --days N limits the per-day section\n"
                "to the most recent N calendar days (default 30; use 0 to show all).\n\n"
                "Pricing is fetched from aws.amazon.com/bedrock/pricing/ (cached for 7 days).\n"
                "Default region: 'US East (N. Virginia)' (matches the wrapper's AWS_REGION=us-east-1).\n\n"
                "Projects are recorded by `agent` on each launch — a project that\n"
                "has never had `agent` invoked from it will not appear here.",
                file=sys.stderr,
            )
            return 0
        if a == "--cache" and i + 1 < len(args):
            cache_path = Path(args[i + 1]); i += 2; continue
        if a == "--region" and i + 1 < len(args):
            region_label = args[i + 1]; i += 2; continue
        if a == "--refresh":
            refresh = True; i += 1; continue
        if a == "--days" and i + 1 < len(args):
            try:
                days_window = int(args[i + 1])
            except ValueError:
                print(f"agent_usage: --days expects an integer, got '{args[i + 1]}'", file=sys.stderr)
                return 1
            if days_window < 0:
                print("agent_usage: --days must be >= 0", file=sys.stderr)
                return 1
            i += 2; continue
        positional.append(a)
        i += 1

    if not positional:
        print("Usage: agent_usage.py [--cache PATH] [--region LABEL] [--refresh] [--days N] <projects.txt>", file=sys.stderr)
        return 1

    reg = Path(positional[0])
    if not reg.is_file():
        print(f"agent_usage: registry not found at {reg}", file=sys.stderr)
        return 1

    projects = load_projects(reg)
    if not projects:
        print("agent_usage: no projects recorded yet — launch `agent` once to register a project.", file=sys.stderr)
        return 0

    prices = load_prices(cache_path, region_label, refresh)

    rows: list[dict] = []
    totals_by_model: dict[str, Bucket] = defaultdict(Bucket)
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(
        lambda: defaultdict(Bucket)
    )
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
        rows.append({
            "path": path,
            "exists": exists,
            "sessions": sessions,
            "last_ts": last_ts,
            "total": total,
            "cost": None if proj_unknown else proj_cost,
        })

    rows.sort(key=lambda r: (r["cost"] if r["cost"] is not None else -1.0), reverse=True)

    print(render(
        rows,
        dict(totals_by_model),
        {d: dict(m) for d, m in totals_by_day_by_model.items()},
        prices,
        days_window,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
