# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from agent_wrap.lib.buckets import Bucket
from agent_wrap.lib.console import Ansi
from agent_wrap.lib.format import (
    epoch_to_dt,
    fmt_count,
)
from agent_wrap.lib.grouping import orphaned_log_dirs, resolve_group
from agent_wrap.lib.models import normalize_model
from agent_wrap.lib.render import render_core
from agent_wrap.lib.tree import (
    DisplayRow,
    build_project_tree,
    flatten_tree,
)
from agent_wrap.lib.usage_args import (
    UsageArgs,
    load_projects,
    parse_usage_args,
)
from agent_wrap.providers import get_provider

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from pathlib import Path

USAGE = "[--days N]"
SUMMARY = "Show token usage stats (reads from .claude/litellm-logs/)"

_MODEL_CONTEXT_SUFFIX_RE = re.compile(r"\[(?:1m|128k|32k|8k)\]$", re.IGNORECASE)


def _best_prefix_key(query: str, keys: Iterable[str]) -> str | None:
    """
    Pick the pricing-table key that best matches `query` under true-prefix matching.

    A key matches when it is a prefix of the query or the query is a prefix of it,
    so a date-stamped request id matches its base pricing key, and a base request
    matches its newest date-stamped key. Among matches prefer, in order:
      1. the longest shared prefix,
      2. then the shortest key (an exact base key beats a longer date-stamped one),
      3. then the alphabetically-greatest key (newer date suffix wins).
    """
    best: str | None = None
    best_rank: tuple[int, int, str] | None = None
    for k in keys:
        if not (k.startswith(query) or query.startswith(k)):
            continue
        # For a true prefix match the shared prefix is the shorter whole string.
        rank = (min(len(k), len(query)), -len(k), k)
        if best_rank is None or rank > best_rank:
            best, best_rank = k, rank
    return best


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


class PriceSource:
    """
    Lazily fetches and serves per-provider pricing as a uniform tiered table.

    A provider's pricing is fetched at most once, on first request for it. The
    public surface is a single method, `get_pricing(provider, model)`, which
    always returns a *tiered* price list (or None when no price is known).

    Flat (non-tiered) provider pricing is recast into a single tier whose
    `max_in` is infinity, so the per-request cost path never needs to know
    whether the underlying provider quotes flat or tiered rates.
    """

    def __init__(self) -> None:
        # provider_name -> {model_key: [tier, ...]}, or None if unavailable.
        # Key presence (even with a None value) marks the provider as fetched.
        self._cache: dict[str, dict[str, list[dict]] | None] = {}

    def get_pricing(self, provider: str, model: str) -> list[dict] | None:
        """Return the tier list for `model`, or None if no price is known."""
        if provider not in self._cache:
            self._cache[provider] = self._fetch(provider)
        table = self._cache[provider]
        if not table:
            return None

        clean = model.rsplit("/", 1)[-1]
        candidates = [
            normalize_model(clean),
            _MODEL_CONTEXT_SUFFIX_RE.sub("", clean),
            clean,
        ]
        for key in candidates:
            if not key:
                continue
            match = _best_prefix_key(key, table)
            if match is not None:
                return table[match]
        return None

    def compute_cost(self, provider: str, model: str, raw_response: dict | None) -> float | None:
        """
        Compute the USD cost of a single request, or None if pricing is unknown.

        Encapsulates the full pipeline: model-to-tier lookup, usage extraction,
        and the per-tier cost formula. Callers that also need the usage dict for
        accumulation should call ``get_pricing`` + ``extract_usage`` +
        ``_cost_for_tiers`` directly instead.
        """
        tiers = self.get_pricing(provider, model.rsplit("/", 1)[-1])
        if tiers is None:
            return None
        usage = extract_usage(raw_response)
        return _cost_for_tiers(tiers, usage)

    def _fetch(self, provider: str) -> dict[str, list[dict]]:
        """Build the unified tiered table for one provider (fetched once)."""
        try:
            p = get_provider(provider)
            flat = p.get_pricing()
            tiered = p.get_tiered_pricing()
        except Exception:  # noqa: BLE001
            return {}

        table: dict[str, list[dict]] = {}
        # Flat rates first, recast as a single infinite tier...
        for model_key, rates in (flat or {}).items():
            table[model_key] = [
                {
                    "max_in": float("inf"),
                    "in": rates["in"],
                    "out": rates["out"],
                    "cw_5m": rates["cw_5m"],
                    "cw_1h": rates["cw_1h"],
                    "cr": rates["cr"],
                }
            ]
        # ...then let genuine tiered pricing override flat for the same model.
        for model_key, entry in (tiered or {}).items():
            if entry and "tiers" in entry:
                table[model_key] = entry["tiers"]
        return table


_usage_convention_warned = False


def _warn_usage_convention_drift(in_tokens: int, cw_5m: int, cw_1h: int, cr_tokens: int) -> None:
    """
    Emit a one-shot warning when the token-overlap assumption appears to have drifted.

    Cost math here assumes prompt/input token counts are INCLUSIVE of cache-write
    and cache-read tokens (the OpenAI/LiteLLM convention these logs use). If fresh
    input ever goes negative, that assumption has broken — most likely a provider or
    LiteLLM change to EXCLUSIVE reporting — and per-request costs are no longer
    trustworthy. Warn once per process so a real regression is visible without
    spamming a line per record.
    """
    global _usage_convention_warned  # noqa: PLW0603
    if _usage_convention_warned:
        return
    _usage_convention_warned = True
    print(
        "warning: token usage convention drift detected — "
        f"input_tokens ({in_tokens}) < cache-write ({cw_5m + cw_1h}) + "
        f"cache-read ({cr_tokens}). Cost math assumes input_tokens is inclusive of "
        "cache tokens; this record violates that. Reported costs may be inaccurate "
        "until agent_wrap/commands/stats.py:_cost_for_tiers is revisited.",
        file=sys.stderr,
    )


def _cost_for_tiers(tiers: list[dict], usage: dict) -> float:
    """Calculate the cost of a single request given its applicable tier list."""
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

    # Pick the first tier covering the total input size, else the highest tier.
    tier = next((t for t in tiers if in_tokens <= t["max_in"]), tiers[-1])

    # `in_tokens` comes from prompt_tokens/input_tokens, which in the LiteLLM logs
    # this tool reads is the INCLUSIVE total: fresh + cache-write + cache-read. We
    # bill cache writes (cw_5m/cw_1h) and cache reads (cr) at their own rates below,
    # so they must be removed here to avoid charging them twice at the full input
    # rate. Fresh input is therefore the remainder once both are subtracted.
    fresh_in_tokens = in_tokens - cw_5m - cw_1h - cr_tokens
    if fresh_in_tokens < 0:
        # The remainder going negative means the inclusivity assumption above no
        # longer holds — e.g. a LiteLLM/provider upgrade switched to reporting
        # input_tokens EXCLUSIVE of the cache fields (the raw Anthropic convention).
        # Either way the cost math here is wrong; surface it once rather than
        # silently clamping and mischarging. See _check_usage_convention's note.
        _warn_usage_convention_drift(in_tokens, cw_5m, cw_1h, cr_tokens)
        fresh_in_tokens = 0

    return (
        fresh_in_tokens * tier["in"] / 1_000_000
        + out_tokens * tier["out"] / 1_000_000
        + cw_5m * tier["cw_5m"] / 1_000_000
        + cw_1h * tier["cw_1h"] / 1_000_000
        + cr_tokens * tier["cr"] / 1_000_000
    )


def _model_display_rows(totals_by_model: dict[str, Bucket]) -> list[DisplayRow]:
    """
    Render the per-model breakdown as a provider/model tree.

    Reuses the same trie machinery as the project tree by treating each
    `provider/model` key as a path that `Path(...).parts` splits on `/`.
    Models carry no session/launch data, so those columns are left blank by
    the callers; only the token/cost columns of each DisplayRow are used.
    """
    rows = [
        {
            "path": model,
            "exists": True,
            "sessions": 0,
            "last_ts": None,
            "total": bucket,
            "cost": None if bucket.cost_unknown else bucket.cost,
        }
        for model, bucket in totals_by_model.items()
    ]
    return flatten_tree(build_project_tree(rows))


def _build_model_section(totals_by_model: dict[str, Bucket], leading_blanks: int) -> list:
    """
    Build the per-model breakdown body rows (provider/model tree).

    `leading_blanks` empty cells follow the label to skip the SESSIONS /
    LAST LAUNCH columns in the Total table (2) versus the Recent table (0).
    """
    blanks = [""] * leading_blanks
    body: list = []
    for dr in _model_display_rows(totals_by_model):
        style = Ansi.DIM if dr.is_structural else Ansi.NONE
        body.append(
            (
                [
                    dr.label,
                    *blanks,
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


def _cost_record(
    rec: dict,
    provider_name: str,
    prices: PriceSource,
    by_day: dict[str, dict[str, Bucket]],
) -> datetime | None:
    """Cost one log record into `by_day`. Returns its timestamp, if any."""
    if rec.get("status") != "success":
        return None
    model = rec.get("model")
    if not model:
        return None

    clean_model = model.rsplit("/", 1)[-1]
    norm_model = normalize_model(clean_model) or clean_model
    display_model = f"{provider_name}/{norm_model}"

    ts = epoch_to_dt((rec.get("timing") or {}).get("start"))
    day_key = ts.astimezone().strftime("%Y-%m-%d") if ts else "?"

    usage = extract_usage(rec.get("response"))
    # Pass the raw result through: None means pricing was unknown for this
    # request, which the Bucket records distinctly from a known-zero cost.
    cost = prices.compute_cost(provider_name, model, rec.get("response"))

    by_day[day_key][display_model].add(usage, cost)
    return ts


def _scan_logs_dir(  # noqa: C901
    logs_dir: Path,
    prices: PriceSource,
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]]]:
    """
    Scan a LiteLLM logs dir (``<provider>/<session>/messages.jsonl``) line-by-line,
    costing each request as it is read. Returns (sessions, last_ts, by_day_by_model).

    Works on both a project's ``.claude/litellm-logs`` symlink and a central
    orphaned ``<hash>`` dir, since they share the same internal layout.
    """
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    last_ts: datetime | None = None
    session_count = 0

    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        provider_name = provider_dir.name

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
                        ts = _cost_record(rec, provider_name, prices, by_day)
                        if ts is not None and (last_ts is None or ts > last_ts):
                            last_ts = ts
            except OSError:
                continue

    return session_count, last_ts, {d: dict(m) for d, m in by_day.items()}


def _scan_project(
    path: Path,
    prices: PriceSource,
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], bool]:
    """
    Scan one project's LiteLLM logs. Returns (sessions, last_ts, by_day, exists).

    ``exists`` is False when the project's ``.claude/litellm-logs`` is gone (a
    deleted project / stale registry entry), in which case nothing is scanned.
    """
    logs_dir = path / ".claude" / "litellm-logs"
    if not logs_dir.is_dir():
        return 0, None, {}, False
    sessions, last_ts, by_day = _scan_logs_dir(logs_dir, prices)
    return sessions, last_ts, by_day, True


def _collect_orphaned(
    tool_dir: Path,
    projects: list[Path],
    prices: PriceSource,
    totals_by_model: dict[str, Bucket],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
) -> dict | None:
    """
    Aggregate central log dirs not reachable from any registered project.

    These are real spend whose project dir is gone, so each request is folded into
    the passed-in per-model and per-day totals (exactly like a project), and a
    single summary ``{"sessions", "last_ts", "total"}`` is returned for the
    synthetic ``<orphaned>`` row. Returns None when there are no orphaned sessions.
    """
    total = Bucket()
    sessions = 0
    last_ts: datetime | None = None

    for logs_dir in orphaned_log_dirs(tool_dir, projects):
        d_sessions, d_last_ts, by_day = _scan_logs_dir(logs_dir, prices)
        sessions += d_sessions
        if d_last_ts is not None and (last_ts is None or d_last_ts > last_ts):
            last_ts = d_last_ts
        for day, by_model in by_day.items():
            for model, b in by_model.items():
                total.merge(b)
                # The totals are the plain dicts returned by _aggregate_projects;
                # orphaned logs may introduce a model/day not seen in any project,
                # so create the bucket on demand rather than assuming it exists.
                totals_by_model.setdefault(model, Bucket()).merge(b)
                totals_by_day_by_model.setdefault(day, {}).setdefault(model, Bucket()).merge(b)

    if sessions == 0:
        return None
    return {"sessions": sessions, "last_ts": last_ts, "total": total}


def render(
    rows: list[dict],
    totals_by_model: dict[str, Bucket],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    days_window: int,
    orphaned: dict | None = None,
) -> str:
    # Per-request cost is baked into `Bucket.cost` during the scan; the bucket's
    # `cost_unknown` flag (set when a billable request had no known price) is the
    # authoritative "?" signal — a 0.0 cost without that flag is a known zero.
    return render_core(
        rows,
        totals_by_model,
        totals_by_day_by_model,
        days_window,
        cost_fn=lambda _model, b: (b.cost, b.cost_unknown),
        build_model_section=_build_model_section,
        orphaned=orphaned,
    )


_USAGE_TEXT = (
    "Usage: agent stats [--days N] <projects.txt>\n\n"
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


_USAGE_LINE = "Usage: agent stats [--days N] <projects.txt>"


def _parse_usage_args(args: list[str]) -> UsageArgs | None:
    return parse_usage_args(args, usage_line=_USAGE_LINE, usage_text=_USAGE_TEXT)


class _Group:
    """Per-transient-project accumulator across one or more physical paths."""

    __slots__ = ("exists", "last_ts", "name", "root", "sessions", "total", "transient")

    def __init__(self, root: Path, name: str, *, transient: bool) -> None:
        self.root = root
        self.name = name
        self.transient = transient
        self.total = Bucket()
        self.sessions = 0
        self.last_ts: datetime | None = None
        self.exists = False


def _aggregate_projects(
    projects: list[Path],
    prices: PriceSource,
) -> tuple[list[dict], dict[str, Bucket], dict[str, dict[str, Bucket]]]:
    """
    Scan every project and roll its buckets up into the render inputs.

    Physical projects sharing a ``.agent_stats_leaf`` group root are merged into
    a single row (see :func:`agent_wrap.lib.grouping.resolve_group`); the global
    per-model and per-day totals are unaffected by grouping and are accumulated
    straight from each project's scan.
    """
    groups: dict[Path, _Group] = {}
    totals_by_model: dict[str, Bucket] = defaultdict(Bucket)
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    for path in projects:
        sessions, last_ts, by_day, exists = _scan_project(path, prices)

        root, name, transient = resolve_group(path)
        group = groups.get(root)
        if group is None:
            group = groups[root] = _Group(root, name, transient=transient)

        group.sessions += sessions
        group.exists = group.exists or exists
        if last_ts is not None and (group.last_ts is None or last_ts > group.last_ts):
            group.last_ts = last_ts

        for day, by_model in by_day.items():
            for model, b in by_model.items():
                group.total.merge(b)
                totals_by_model[model].merge(b)
                totals_by_day_by_model[day][model].merge(b)

    rows: list[dict] = []
    for group in groups.values():
        # total.cost is the sum of per-request costs computed during the scan;
        # `cost_unknown` (set when a billable request had no known price) marks
        # the cost as "?", keeping a known-zero total (e.g. all requests errored
        # out) distinct from genuinely-unknown pricing.
        proj_cost = None if group.total.cost_unknown else group.total.cost
        if group.sessions > 0 or group.exists:
            rows.append(
                {
                    "path": group.root,
                    "name": group.name,
                    "transient": group.transient,
                    "exists": group.exists,
                    "sessions": group.sessions,
                    "last_ts": group.last_ts,
                    "total": group.total,
                    "cost": proj_cost,
                }
            )

    rows.sort(key=lambda r: r["cost"] if r["cost"] is not None else -1.0, reverse=True)
    return (
        rows,
        dict(totals_by_model),
        {d: dict(m) for d, m in totals_by_day_by_model.items()},
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

    prices = PriceSource()
    rows, totals_by_model, totals_by_day_by_model = _aggregate_projects(projects, prices)

    # Filter out projects with no logs
    rows = [r for r in rows if r["sessions"] > 0]

    # Logs left behind by deleted projects / stale registry entries surface under
    # a synthetic <orphaned> row; their usage also folds into the totals above.
    orphaned = _collect_orphaned(
        tool_dir, projects, prices, totals_by_model, totals_by_day_by_model
    )

    if not rows and orphaned is None:
        print("usage: no LiteLLM logs found for any registered project.", file=sys.stderr)
        return 0

    print(
        render(
            rows,
            totals_by_model,
            totals_by_day_by_model,
            parsed.days_window,
            orphaned=orphaned,
        )
    )
    return 0
