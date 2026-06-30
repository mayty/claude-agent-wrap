# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any

from agent_wrap.lib.buckets import Bucket
from agent_wrap.lib.console import Ansi
from agent_wrap.lib.format import (
    day_in_range,
    epoch_to_dt,
    fmt_cost_with_unknown,
    fmt_count,
)
from agent_wrap.lib.grouping import orphaned_log_dirs, resolve_group
from agent_wrap.lib.models import normalize_model
from agent_wrap.lib.render import range_label, render_core
from agent_wrap.lib.table import RowItem, compute_shared_widths, render_table
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

USAGE = "[-v|--verbose] [-f|--from D] [-u|--until D] [-d|--days N]"
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


def _collect_cache_ttls(node: Any, out: set[str]) -> None:
    """Recursively gather every ``cache_control`` breakpoint's TTL into ``out``."""
    if isinstance(node, dict):
        cc = node.get("cache_control")
        if isinstance(cc, dict):
            # Per the Anthropic/Bedrock protocol, a "ttl": "1h" field selects the
            # 1-hour cache; its absence (or "5m") means the 5-minute default.
            out.add("1h" if cc.get("ttl") == "1h" else "5m")
        for key, value in node.items():
            if key == "cache_control":
                continue
            _collect_cache_ttls(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_cache_ttls(item, out)


def request_cache_ttl(request: dict[str, Any] | None) -> str | None:
    """
    Determine the cache-write TTL tier a request asked for from its markers.

    The real Anthropic request lives at ``request.body.data`` (see
    ``providers/litellm_common/callback.py``). Each cache breakpoint carries a
    ``cache_control`` object whose optional ``"ttl": "1h"`` selects the 1-hour
    cache (absent → 5-minute default). Bedrock/LiteLLM responses report only a
    flat cache-write total with no 5m/1h split, so this request-side marker is the
    authoritative source for attributing that total to a tier.

    Returns "5m" or "1h" when all breakpoints agree, "mixed" when they disagree,
    or None when the request carries no ``cache_control`` markers at all.
    """
    if not isinstance(request, dict):
        return None
    body = request.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    ttls: set[str] = set()
    _collect_cache_ttls(data, ttls)
    if not ttls:
        return None
    if len(ttls) > 1:
        return "mixed"
    return ttls.pop()


def _response_cache_split(usage: dict) -> dict[str, int]:
    """
    Read the response's ephemeral 5m/1h cache-write split, if it reports one.

    Anthropic documents the split nested under ``usage.cache_creation``; some paths
    surface it as top-level ``usage`` keys instead. Both shapes are read (nested
    first, top-level overriding) so a response-provided split is authoritative over
    request-TTL inference. Returns an empty dict when no split is present.
    """
    split: dict[str, int] = {}
    for source in (usage.get("cache_creation"), usage):
        if not isinstance(source, dict):
            continue
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            if key in source:
                split[key] = source[key]
    return split


def extract_usage(response: dict | None, request_ttl: str | None = None) -> dict[str, Any]:
    """
    Extract and normalize usage dict from a LiteLLM response object.

    ``request_ttl`` (from :func:`request_cache_ttl`) attributes the cache-write
    tokens to a 5m/1h tier when the response itself omits the split.
    """
    if not response or not isinstance(response, dict):
        return {}
    usage = response.get("usage")
    if not usage or not isinstance(usage, dict):
        return {}

    cache_creation = _response_cache_split(usage)

    # LiteLLM standardizes to prompt_tokens/completion_tokens, but some
    # providers or older versions might use input_tokens/output_tokens.
    in_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    cw_tokens = usage.get("cache_creation_input_tokens") or 0
    cr_tokens = usage.get("cache_read_input_tokens") or 0

    # When the response gave no ephemeral breakdown (the Bedrock/LiteLLM case),
    # attribute the flat cache-write total to the tier the request asked for. A
    # request that mixed TTLs across breakpoints can't be split from a single
    # total, so it falls through to the 5m default with a one-shot warning.
    if not cache_creation and cw_tokens and request_ttl:
        if request_ttl == "mixed":
            _warn_mixed_cache_ttl()
        elif request_ttl == "1h":
            cache_creation["ephemeral_1h_input_tokens"] = cw_tokens
        else:
            cache_creation["ephemeral_5m_input_tokens"] = cw_tokens

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

    def compute_cost(
        self,
        provider: str,
        model: str,
        raw_response: dict[str, Any] | None,
        request_ttl: str | None = None,
    ) -> float | None:
        """
        Compute the USD cost of a single request, or None if pricing is unknown.

        Encapsulates the full pipeline: model-to-tier lookup, usage extraction,
        and the per-tier cost formula. ``request_ttl`` (from
        :func:`request_cache_ttl`) attributes cache writes to a 5m/1h tier when
        the response omits the split. Callers that also need the usage dict for
        accumulation should call ``get_pricing`` + ``extract_usage`` +
        ``_cost_for_tiers`` directly instead.
        """
        tiers = self.get_pricing(provider, model.rsplit("/", 1)[-1])
        if tiers is None:
            return None
        usage = extract_usage(raw_response, request_ttl)
        return _cost_for_tiers(tiers, usage)

    def _fetch(self, provider: str) -> dict[str, list[dict[str, float]]]:
        """Build the unified tiered table for one provider (fetched once)."""
        try:
            p = get_provider(provider)
            flat = p.get_pricing()
            tiered = p.get_tiered_pricing()
        except Exception:  # noqa: BLE001
            return {}

        table: dict[str, list[dict[str, float]]] = {}
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
_mixed_cache_ttl_warned = False

# When False, the warn-* helpers below record into `_warn_capture` instead of
# printing. Pool workers run with this off (set in `_worker_init`) so their
# warnings travel back to the parent — which prints each exactly once — rather
# than being lost to a child stderr or duplicated once per worker process. The
# in-process/serial path leaves it True and prints inline as before.
_warn_print = True
_warn_capture: dict[str, Any] = {}


def _warn_mixed_cache_ttl() -> None:
    """
    Emit a one-shot warning when a request mixed 5m and 1h cache TTLs.

    The response reports only a flat cache-write total, so when a single request
    used both 5-minute and 1-hour cache breakpoints we cannot split that total
    between the two tiers. Such requests fall back to the 5m rate; warn once so
    the (rare) imprecision is visible without spamming a line per record.
    """
    global _mixed_cache_ttl_warned  # noqa: PLW0603
    if not _warn_print:
        _warn_capture["mixed"] = True
        return
    if _mixed_cache_ttl_warned:
        return
    _mixed_cache_ttl_warned = True
    print(
        "warning: a request mixed 5m and 1h cache TTLs, but the response reports "
        "only a flat cache-write total. Those writes are priced at the 5m rate; "
        "reported cache-write cost may be slightly low. See "
        "agent_wrap/commands/stats.py:request_cache_ttl.",
        file=sys.stderr,
    )


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
    if not _warn_print:
        # The first drift's numbers are representative enough for the parent's
        # single emission; later ones (same root cause) need not be threaded back.
        _warn_capture.setdefault("drift", (in_tokens, cw_5m, cw_1h, cr_tokens))
        return
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

    # `cache_creation` is populated either from the response's ephemeral split or,
    # when that's absent, from the request's cache_control TTL (see extract_usage /
    # request_cache_ttl). Only when neither source resolved a tier do we fall back
    # to charging the flat total at the 5m rate.
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


def _build_model_section(totals_by_model: dict[str, Bucket], leading_blanks: int) -> list[RowItem]:
    """
    Build the per-model breakdown body rows (provider/model tree).

    `leading_blanks` empty cells follow the label to skip the SESSIONS /
    LAST LAUNCH columns in the Total table (2) versus the Recent table (0).
    """
    blanks = [""] * leading_blanks
    body: list[RowItem] = []
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


def _cost_record(  # noqa: PLR0913
    rec: dict[str, Any],
    provider_name: str,
    prices: PriceSource,
    by_day: dict[str, dict[str, Bucket]],
    by_day_by_source: dict[str, dict[str, Bucket]] | None = None,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
) -> tuple[bool, datetime | None]:
    """
    Cost one log record into `by_day`. Returns ``(accumulated, timestamp)``.

    ``accumulated`` is True when the record was a billable success whose day fell
    within the inclusive ``[from_iso, until_iso]`` window — used to decide whether
    its session should be counted. Out-of-window or non-success records are skipped
    (nothing accumulated, ``(False, None)``). ``timestamp`` is the record's start
    time when it has one (None for accumulated-but-timestamp-less records, which
    only occur in the all-time view). When `by_day_by_source` is provided, the same
    usage/cost is also accumulated into ``by_day_by_source[day_key][source]``
    (source from :func:`_usage_source`) so the verbose breakdown can attribute
    totals to their provenance.
    """
    if rec.get("status") != "success":
        return False, None
    model = rec.get("model")
    if not model:
        return False, None

    ts = epoch_to_dt((rec.get("timing") or {}).get("start"))
    day_key = ts.astimezone().strftime("%Y-%m-%d") if ts else "?"
    if not day_in_range(day_key, from_iso, until_iso):
        return False, None

    clean_model = model.rsplit("/", 1)[-1]
    norm_model = normalize_model(clean_model) or clean_model
    display_model = f"{provider_name}/{norm_model}"

    # The request's cache_control TTL attributes the flat cache-write total to a
    # 5m/1h tier when the response omits the breakdown (the Bedrock case). It is
    # *only* consulted in that case, so resolving it eagerly for every record
    # would walk each request body needlessly — by far the hottest cost in a
    # large scan. Compute it lazily: only when the response carries a flat
    # cache-write total with no ephemeral split of its own does the request-side
    # marker matter. See request_cache_ttl / extract_usage / _response_cache_split.
    response = rec.get("response")
    request_ttl = None
    resp_usage = response.get("usage") if isinstance(response, dict) else None
    if (
        isinstance(resp_usage, dict)
        and (resp_usage.get("cache_creation_input_tokens") or 0)
        and not _response_cache_split(resp_usage)
    ):
        request_ttl = request_cache_ttl(rec.get("request"))

    usage = extract_usage(response, request_ttl)
    # Price the usage we just extracted directly, rather than calling
    # prices.compute_cost — which would re-run extract_usage internally — so the
    # response is normalized exactly once per record. get_pricing returning None
    # means pricing was unknown for this request, which the Bucket records
    # distinctly from a known-zero cost.
    tiers = prices.get_pricing(provider_name, model)
    cost = None if tiers is None else _cost_for_tiers(tiers, usage)

    unrecorded = _usage_unrecorded(rec)
    by_day[day_key][display_model].add(usage, cost, unrecorded=unrecorded)
    if by_day_by_source is not None:
        by_day_by_source[day_key][_usage_source(rec)].add(usage, cost, unrecorded=unrecorded)
    return True, ts


_USAGE_SOURCES = ("native", "standard_logging_object", "unrecoverable")


def _usage_source(rec: dict[str, Any]) -> str:
    """
    Classify how a success record's usage was obtained, for the verbose breakdown.

    Mirrors the three outcomes the callback's ``_usable_response`` stamps onto a
    record's ``response`` (see ``providers/litellm_common/callback.py``):
      * ``"native"`` — a parsed response dict with no ``_usage_source`` key (usage
        came straight from the response);
      * ``"standard_logging_object"`` — dict tagged with that source (usage was
        recovered from LiteLLM's standard logging object fallback);
      * ``"unrecoverable"`` — dict tagged ``"unrecoverable"``, or a bare legacy
        ``"<Response ...>"`` string; no usable usage at all.
    """
    response = rec.get("response")
    if isinstance(response, str):
        return "unrecoverable"
    if isinstance(response, dict):
        src = response.get("_usage_source")
        if src in ("standard_logging_object", "unrecoverable"):
            return src
        return "native"
    return "unrecoverable"


def _usage_unrecorded(rec: dict[str, Any]) -> bool:
    """
    Report whether a success record carries no usable usage to cost.

    Two shapes qualify: legacy logs whose ``response`` is a bare stringified HTTP
    object (e.g. ``"<Response [200 OK]>"``, written before the callback recovered
    usage from the standard logging object), and post-fix records the callback
    explicitly tagged ``"_usage_source": "unrecoverable"``. Either way the request
    was real but its tokens are missing, so it should be counted, not hidden as a
    silent $0 row. See ``providers/litellm_common/callback.py:_usable_response``.
    Delegates to :func:`_usage_source` so the two classifications can't drift.
    """
    return _usage_source(rec) == "unrecoverable"


def _file_predates_range(messages_file: Path, from_iso: str | None) -> bool:
    """
    Report whether a log file can be culled (skipped unread) for the lower bound.

    LiteLLM message logs are append-only and written live, so every record's event
    time is ``<= the file's mtime``. If the file's mtime (as a host-local date) is
    before ``from_iso``, no record in it can be in range, so it is safe to skip
    without parsing. Only the lower bound is cullable this way — a recent mtime
    says nothing about a file's *earliest* record, so ``--until`` cannot cull via
    metadata. Returns False when ``from_iso`` is None (no lower bound) or on any
    stat error (fall through to parsing, which stays the authority).
    """
    if from_iso is None:
        return False
    try:
        mtime = messages_file.stat().st_mtime
    except OSError:
        return False
    mtime_day = epoch_to_dt(mtime)
    if mtime_day is None:
        return False
    return mtime_day.astimezone().strftime("%Y-%m-%d") < from_iso


def _enumerate_session_files(logs_dir: Path, from_iso: str | None) -> list[tuple[str, Path]]:
    """
    List a logs dir's ``(provider_name, messages.jsonl)`` units, mtime-culled.

    Walks the ``<provider>/<session>/`` layout shared by a project's
    ``.claude/litellm-logs`` symlink and a central orphaned ``<hash>`` dir, and
    drops files whose mtime predates the lower bound (see
    :func:`_file_predates_range`) so culled files never become scan work. This is
    the cheap metadata-only pass; the costly per-file parsing happens in
    :func:`_scan_session_file`, which lets the work be fanned out across processes.
    """
    units: list[tuple[str, Path]] = []
    try:
        provider_dirs = list(logs_dir.iterdir())
    except OSError:
        return units
    for provider_dir in provider_dirs:
        if not provider_dir.is_dir():
            continue
        provider_name = provider_dir.name
        try:
            session_dirs = provider_dir.iterdir()
        except OSError:
            continue
        for session_dir in session_dirs:
            if not session_dir.is_dir():
                continue
            messages_file = session_dir / "messages.jsonl"
            if not messages_file.is_file():
                continue
            if _file_predates_range(messages_file, from_iso):
                continue
            units.append((provider_name, messages_file))
    return units


# A single session file's contribution: (had_in_window_record, last_ts, by_day,
# by_day_by_source). The two dicts are plain (not defaultdict) so the result
# pickles cleanly back from a pool worker.
_FileResult = tuple[
    bool, "datetime | None", dict[str, dict[str, Bucket]], dict[str, dict[str, Bucket]]
]


def _scan_session_file(
    provider_name: str,
    messages_file: Path,
    prices: PriceSource,
    *,
    from_iso: str | None,
    until_iso: str | None,
) -> _FileResult:
    """
    Cost one ``messages.jsonl`` line-by-line. Returns this file's
    ``(had_record, last_ts, by_day_by_model, by_day_by_source)``.

    The single per-file scanning core shared by the serial path
    (:func:`_scan_logs_dir`) and the parallel worker (:func:`_scan_one_file`), so
    the two can never drift. Only in-window records are counted; ``had_record`` is
    True when at least one such record was costed (so the dir can count the
    session). ``by_day_by_source`` mirrors ``by_day`` keyed by usage source (see
    :func:`_usage_source`) for the verbose breakdown.
    """
    by_day: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    by_day_by_source: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    last_ts: datetime | None = None
    had_record = False
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
                accumulated, ts = _cost_record(
                    rec,
                    provider_name,
                    prices,
                    by_day,
                    by_day_by_source,
                    from_iso=from_iso,
                    until_iso=until_iso,
                )
                had_record = had_record or accumulated
                if ts is not None and (last_ts is None or ts > last_ts):
                    last_ts = ts
    except OSError:
        return False, None, {}, {}
    return (
        had_record,
        last_ts,
        {d: dict(m) for d, m in by_day.items()},
        {d: dict(s) for d, s in by_day_by_source.items()},
    )


def _merge_by_day(dst: dict[str, dict[str, Bucket]], src: dict[str, dict[str, Bucket]]) -> None:
    """Merge one ``by_day[day][key] -> Bucket`` map into another in place."""
    for day, by_key in src.items():
        dst_day = dst.setdefault(day, {})
        for key, bucket in by_key.items():
            existing = dst_day.get(key)
            if existing is None:
                dst_day[key] = bucket
            else:
                existing.merge(bucket)


def _fold_file_results(
    results: Iterable[_FileResult],
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], dict[str, dict[str, Bucket]]]:
    """
    Fold per-file results into a dir aggregate: (sessions, last_ts, by_day, by_source).

    A session is counted only when its file contributed at least one in-window
    record, so the session column reflects the selected range rather than all-time
    directories — matching the original per-dir scan.
    """
    by_day: dict[str, dict[str, Bucket]] = {}
    by_day_by_source: dict[str, dict[str, Bucket]] = {}
    last_ts: datetime | None = None
    session_count = 0
    for had_record, ts, file_by_day, file_by_source in results:
        if had_record:
            session_count += 1
        if ts is not None and (last_ts is None or ts > last_ts):
            last_ts = ts
        _merge_by_day(by_day, file_by_day)
        _merge_by_day(by_day_by_source, file_by_source)
    return session_count, last_ts, by_day, by_day_by_source


# --- Parallel scan -----------------------------------------------------------
#
# The scan is embarrassingly parallel per session file, and json.loads (CPU-bound
# C that holds the GIL) dominates it, so a process pool — not threads — is what
# wins. `run` enumerates every file across all projects and orphaned dirs, fans
# them through one shared pool, and folds the per-dir results back. Below a
# threshold the pool's fork/import cost isn't worth it, so the serial path runs.

# A unit of parallel work: (dir_index, provider_name, messages_file). dir_index
# tags which logs dir the file belongs to so the parent can fold results per dir
# (a project's sessions vs. an orphaned dir's) without the worker knowing.
_WorkUnit = tuple[int, str, "Path"]

# Below this many files, fork + interpreter import per worker costs more than the
# serial scan saves. Tuned well above an empty/near-empty scan (~50ms serial).
_PARALLEL_MIN_FILES = 64

# Set in each worker process by `_worker_init`. The pool reuses one PriceSource
# (and its lazily-fetched provider tables) per process rather than pickling it
# per task — pricing fetch is local and cheap, so a fresh one per worker is fine.
# The window bounds are passed once via initargs (not per task) since they are
# constant across the whole scan.
_worker_prices: PriceSource | None = None
_worker_from_iso: str | None = None
_worker_until_iso: str | None = None


def plan_pool(nfiles: int) -> tuple[int, int]:
    """
    Choose ``(workers, chunksize)`` for a parallel scan of ``nfiles`` files.

    Sized to the machine and the workload, validated against a chunksize-by-pool
    sweep on a 25.5K-record dataset:
      * workers — ``min(cpu_count, 8, ceil(nfiles / 16))``. Decode saturates
        ~8 workers (16 was no faster), so 8 is the cap; it also scales *down* on
        few-core hosts and small datasets (no point forking 8 for 20 files).
      * chunksize — ``max(1, min(8, nfiles // (workers * 4)))``, ≈4 chunks per
        worker. ``map`` dispatches chunks lazily as workers free up, so several
        small chunks per worker keep load balanced when a few sessions are far
        larger than the rest; the [1, 8] clamp matches the sweep's flat optimum.
    """
    cpu = os.cpu_count() or 1
    workers = max(1, min(cpu, 8, math.ceil(nfiles / 16)))
    chunksize = max(1, min(8, nfiles // (workers * 4)))
    return workers, chunksize


def _worker_init(from_iso: str | None, until_iso: str | None) -> None:
    """Set up one pool worker: own PriceSource, window bounds, warn-capture mode."""
    global _worker_prices, _worker_from_iso, _worker_until_iso, _warn_print  # noqa: PLW0603
    _worker_prices = PriceSource()
    _worker_from_iso = from_iso
    _worker_until_iso = until_iso
    # Warnings printed from a child go to a stderr the user never sees and would
    # also duplicate per worker; capture them instead so the parent emits once.
    _warn_print = False


def _scan_one_file(unit: _WorkUnit) -> tuple[int, _FileResult, bool, tuple | None]:
    """
    Pool task: scan one file with the worker's PriceSource. Returns
    ``(dir_index, file_result, saw_mixed_ttl, drift_numbers_or_None)``.

    The two warning signals ride back with the result so the parent can emit each
    at most once (workers run with printing suppressed; see :func:`_worker_init`).
    """
    global _warn_capture  # noqa: PLW0603
    dir_index, provider_name, messages_file = unit
    assert _worker_prices is not None  # set by _worker_init
    _warn_capture = {}
    result = _scan_session_file(
        provider_name,
        messages_file,
        _worker_prices,
        from_iso=_worker_from_iso,
        until_iso=_worker_until_iso,
    )
    return dir_index, result, bool(_warn_capture.get("mixed")), _warn_capture.get("drift")


# A scan cache maps each logs dir to its folded
# (sessions, last_ts, by_day_by_model, by_day_by_source) result, so the
# aggregation pass can look results up instead of re-scanning. `run` builds one
# (possibly in parallel) and threads it through; direct callers that pass None
# scan serially on demand.
_DirResult = tuple[
    int, "datetime | None", dict[str, dict[str, Bucket]], dict[str, dict[str, Bucket]]
]
ScanCache = dict["Path", _DirResult]


def _scan_dirs(
    logs_dirs: list[Path],
    *,
    from_iso: str | None,
    until_iso: str | None,
) -> ScanCache:
    """
    Scan many logs dirs and return a ``{logs_dir: folded_result}`` cache.

    Enumerates every session file across all dirs up front (cheap, metadata-only),
    then fans the per-file scans across a process pool when there are enough files
    to outweigh the pool's startup cost (see :data:`_PARALLEL_MIN_FILES`),
    otherwise scans serially in-process. Either way the per-file results are
    folded back per originating dir with :func:`_fold_file_results`, so the result
    is identical to calling :func:`_scan_logs_dir` on each dir — only faster.
    """
    # Enumerate (dir_index, provider, file) units; dir_index ties each file back
    # to its dir so per-dir folding is exact regardless of scan order.
    units: list[_WorkUnit] = []
    for idx, logs_dir in enumerate(logs_dirs):
        for provider_name, messages_file in _enumerate_session_files(logs_dir, from_iso):
            units.append((idx, provider_name, messages_file))

    # Per-dir result buckets, keyed by dir_index, collecting raw file results.
    per_dir: list[list[_FileResult]] = [[] for _ in logs_dirs]

    if len(units) < _PARALLEL_MIN_FILES:
        # Serial: small enough that a pool's fork/import cost would dominate.
        prices = PriceSource()
        for idx, provider_name, messages_file in units:
            per_dir[idx].append(
                _scan_session_file(
                    provider_name, messages_file, prices, from_iso=from_iso, until_iso=until_iso
                )
            )
    else:
        workers, chunksize = plan_pool(len(units))
        saw_mixed = False
        drift: tuple | None = None
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_worker_init, initargs=(from_iso, until_iso)
        ) as pool:
            for idx, result, file_mixed, file_drift in pool.map(
                _scan_one_file, units, chunksize=chunksize
            ):
                per_dir[idx].append(result)
                saw_mixed = saw_mixed or file_mixed
                if drift is None and file_drift is not None:
                    drift = file_drift
        # Emit each warning once in the parent — workers ran with printing off.
        if saw_mixed:
            _warn_mixed_cache_ttl()
        if drift is not None:
            _warn_usage_convention_drift(*drift)

    return {logs_dir: _fold_file_results(per_dir[idx]) for idx, logs_dir in enumerate(logs_dirs)}


def _scan_logs_dir(
    logs_dir: Path,
    prices: PriceSource,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], dict[str, dict[str, Bucket]]]:
    """
    Scan a LiteLLM logs dir (``<provider>/<session>/messages.jsonl``) line-by-line,
    costing each in-window request as it is read. Returns
    (sessions, last_ts, by_day_by_model, by_day_by_source).

    Only records whose day falls within ``[from_iso, until_iso]`` are counted, and
    a session is counted only when it contributed at least one such record. Files
    whose mtime predates the lower bound are skipped unread (see
    :func:`_file_predates_range`).

    ``by_day_by_source`` mirrors ``by_day_by_model`` but keys the inner dict by
    usage source (see :func:`_usage_source`), feeding the verbose breakdown.

    Works on both a project's ``.claude/litellm-logs`` symlink and a central
    orphaned ``<hash>`` dir, since they share the same internal layout. This is the
    serial path; the parallel scan in :func:`run` fans the same per-file core
    (:func:`_scan_session_file`) across processes and folds with
    :func:`_fold_file_results`.
    """
    results = [
        _scan_session_file(
            provider_name, messages_file, prices, from_iso=from_iso, until_iso=until_iso
        )
        for provider_name, messages_file in _enumerate_session_files(logs_dir, from_iso)
    ]
    return _fold_file_results(results)


def _scan_project(
    path: Path,
    prices: PriceSource,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
    scan_cache: ScanCache | None = None,
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], dict[str, dict[str, Bucket]], bool]:
    """
    Scan one project's LiteLLM logs. Returns
    (sessions, last_ts, by_day, by_day_by_source, exists).

    ``exists`` is False when the project's ``.claude/litellm-logs`` is gone (a
    deleted project / stale registry entry), in which case nothing is scanned.
    When ``scan_cache`` is given, this dir's pre-scanned result is reused instead
    of scanning on demand (see :func:`_scan_dirs`).
    """
    logs_dir = path / ".claude" / "litellm-logs"
    if not logs_dir.is_dir():
        return 0, None, {}, {}, False
    if scan_cache is not None:
        sessions, last_ts, by_day, by_day_by_source = scan_cache.get(logs_dir, (0, None, {}, {}))
    else:
        sessions, last_ts, by_day, by_day_by_source = _scan_logs_dir(
            logs_dir, prices, from_iso=from_iso, until_iso=until_iso
        )
    return sessions, last_ts, by_day, by_day_by_source, True


def _collect_orphaned(  # noqa: PLR0913
    tool_dir: Path,
    projects: list[Path],
    prices: PriceSource,
    totals_by_model: dict[str, Bucket],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    totals_by_day_by_source: dict[str, dict[str, Bucket]] | None = None,
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
    scan_cache: ScanCache | None = None,
) -> dict[str, Any] | None:
    """
    Aggregate central log dirs not reachable from any registered project.

    These are real spend whose project dir is gone, so each request is folded into
    the passed-in per-model and per-day totals (exactly like a project), and a
    single summary ``{"sessions", "last_ts", "total"}`` is returned for the
    synthetic ``<orphaned>`` row. Returns None when there are no orphaned sessions.

    When ``totals_by_day_by_source`` is given, orphaned spend is also folded into
    the per-day per-source breakdown so the verbose table stays consistent. When
    ``scan_cache`` is given, each orphaned dir's pre-scanned result is reused
    instead of scanning on demand (see :func:`_scan_dirs`).
    """
    total = Bucket()
    sessions = 0
    last_ts: datetime | None = None

    for logs_dir in orphaned_log_dirs(tool_dir, projects):
        if scan_cache is not None:
            d_sessions, d_last_ts, by_day, by_day_by_source = scan_cache.get(
                logs_dir, (0, None, {}, {})
            )
        else:
            d_sessions, d_last_ts, by_day, by_day_by_source = _scan_logs_dir(
                logs_dir, prices, from_iso=from_iso, until_iso=until_iso
            )
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
        if totals_by_day_by_source is not None:
            for day, by_source in by_day_by_source.items():
                for source, b in by_source.items():
                    totals_by_day_by_source.setdefault(day, {}).setdefault(source, Bucket()).merge(
                        b
                    )

    if sessions == 0:
        return None
    return {"sessions": sessions, "last_ts": last_ts, "total": total}


def render(
    rows: list[dict[str, Any]],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    from_iso: str | None,
    until_iso: str | None,
    orphaned: dict[str, Any] | None = None,
) -> str:
    # Per-request cost is baked into `Bucket.cost` during the scan; the bucket's
    # `cost_unknown` flag (set when a billable request had no known price) is the
    # authoritative "?" signal — a 0.0 cost without that flag is a known zero.
    return render_core(
        rows,
        totals_by_day_by_model,
        from_iso,
        until_iso,
        cost_fn=lambda _model, b: (b.cost, b.cost_unknown),
        build_model_section=_build_model_section,
        orphaned=orphaned,
    )


def render_source_breakdown(
    totals_by_day_by_source: dict[str, dict[str, Bucket]],
    from_iso: str | None,
    until_iso: str | None,
) -> str:
    """
    Render the verbose "usage source breakdown" table for the selected window.

    One row per usage source (native / standard_logging_object / unrecoverable,
    see :func:`_usage_source`) showing how much of the reported totals came
    straight from responses vs. were recovered from LiteLLM's standard logging
    object fallback vs. were lost — so a reader can judge how far the headline
    cost depends on the recovery path. The source dict is already restricted to
    the window at scan time, so every day present is merged here; rendered
    standalone (its own column widths) since it prints after the main tables.

    Cost reads ``Bucket.cost`` / ``cost_unknown`` directly, valid here because
    ``stats`` bakes per-request cost into the bucket at scan time (same basis as
    :func:`render`). Returns "" when no source has activity in the window.
    """
    merged: dict[str, Bucket] = {}
    for by_source in totals_by_day_by_source.values():
        for source, b in by_source.items():
            merged.setdefault(source, Bucket()).merge(b)

    headers = ["SOURCE", "MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    aligns = ["<", ">", ">", ">", ">", ">", ">"]
    div = "__div__"

    def _row(label: str, b: Bucket, style: Ansi) -> RowItem:
        return (
            [
                label,
                fmt_count(b.msgs),
                fmt_count(b.in_),
                fmt_count(b.out),
                fmt_count(b.cw),
                fmt_count(b.cr),
                fmt_cost_with_unknown(b.cost, unknown=b.cost_unknown),
            ],
            style,
            0,
        )

    body: list[RowItem] = []
    total = Bucket()
    for source in _USAGE_SOURCES:
        b = merged.get(source)
        # Unrecoverable rows carry msgs but zero tokens; the msgs guard keeps them
        # (intentionally surfaced) while dropping sources with no activity at all.
        if b is None or b.msgs == 0:
            continue
        total.merge(b)
        body.append(_row(source, b, Ansi.NONE))

    if not body:
        return ""

    body.append(div)
    body.append(_row("TOTAL", total, Ansi.BOLD_YELLOW))

    shared_widths = compute_shared_widths([(headers, body, 1)], 6)
    title = f"Usage source breakdown ({range_label(from_iso, until_iso)}):"
    return "\n".join(render_table(title, headers, aligns, body, 1, shared_widths))


_USAGE_TEXT = (
    "Usage: agent stats [-v|--verbose] [-f|--from D] [-u|--until D] [-d|--days N] <projects.txt>\n\n"
    "Reads a list of project paths (one per line) and prints aggregated\n"
    "usage stats from each project's .claude/litellm-logs/ directories.\n\n"
    "Output is a per-project table plus a per-model and per-day breakdown,\n"
    "both over the same usage window. Models are displayed as <provider>/<model>.\n"
    "Day buckets use host-local time.\n\n"
    "Selection range (at most two of --from/--until/--days may be combined):\n"
    "  -f, --from D    inclusive lower bound; D is YYYY-MM-DD or -Nd (e.g. -14d)\n"
    "  -u, --until D   inclusive upper bound; same format as --from\n"
    "  -d, --days N    span in days; N=0 means unlimited (no day bound)\n"
    "Defaults: no flags → last 28 days; --from alone → [from, now];\n"
    "--days N alone → last N days [now-(N-1), now]; --until alone → 28 days\n"
    "ending at until; --days 0 alone → all time [open, now].\n\n"
    "-v/--verbose adds a usage-source breakdown table over the same window,\n"
    "splitting totals by how each record's usage was obtained\n"
    "(native response vs. standard_logging_object recovery vs. unrecoverable).\n\n"
    "Pricing is fetched dynamically per-provider as logs are scanned.\n\n"
    "Projects are recorded by `agent` on each launch — a project that\n"
    "has never had `agent` invoked from it will not appear here."
)


_USAGE_LINE = (
    "Usage: agent stats [-v|--verbose] [-f|--from D] [-u|--until D] [-d|--days N] <projects.txt>"
)


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
    *,
    from_iso: str | None = None,
    until_iso: str | None = None,
    scan_cache: ScanCache | None = None,
) -> tuple[
    list[dict],
    dict[str, Bucket],
    dict[str, dict[str, Bucket]],
    dict[str, dict[str, Bucket]],
]:
    """
    Scan every project and roll its in-window buckets up into the render inputs.

    Returns (rows, totals_by_model, totals_by_day_by_model, totals_by_day_by_source),
    all restricted to the inclusive ``[from_iso, until_iso]`` window.
    Physical projects sharing a ``.agent_stats_leaf`` group root are merged into
    a single row (see :func:`agent_wrap.lib.grouping.resolve_group`); the global
    per-model, per-day, and per-source totals are unaffected by grouping and are
    accumulated straight from each project's scan. When ``scan_cache`` is given,
    each project's pre-scanned result is reused instead of scanning on demand
    (see :func:`_scan_dirs`).
    """
    groups: dict[Path, _Group] = {}
    totals_by_model: dict[str, Bucket] = defaultdict(Bucket)
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    totals_by_day_by_source: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    for path in projects:
        sessions, last_ts, by_day, by_day_by_source, exists = _scan_project(
            path, prices, from_iso=from_iso, until_iso=until_iso, scan_cache=scan_cache
        )

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

        for day, by_source in by_day_by_source.items():
            for source, b in by_source.items():
                totals_by_day_by_source[day][source].merge(b)

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
        {d: dict(s) for d, s in totals_by_day_by_source.items()},
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

    # Scan every logs dir — projects and orphaned alike — in one pass up front,
    # fanned across a process pool when the file count warrants it (see
    # _scan_dirs). The aggregation/orphaned passes below then read folded results
    # from this cache instead of scanning serially. orphaned_log_dirs is cheap
    # (metadata only) and is called again inside _collect_orphaned; recomputing it
    # here keeps the dir set authoritative there rather than threading it through.
    orphaned_dirs = orphaned_log_dirs(tool_dir, projects)
    project_log_dirs = [p / ".claude" / "litellm-logs" for p in projects]
    scan_cache = _scan_dirs(
        [*project_log_dirs, *orphaned_dirs],
        from_iso=parsed.from_iso,
        until_iso=parsed.until_iso,
    )

    rows, totals_by_model, totals_by_day_by_model, totals_by_day_by_source = _aggregate_projects(
        projects,
        prices,
        from_iso=parsed.from_iso,
        until_iso=parsed.until_iso,
        scan_cache=scan_cache,
    )

    # Filter out projects with no logs
    rows = [r for r in rows if r["sessions"] > 0]

    # Logs left behind by deleted projects / stale registry entries surface under
    # a synthetic <orphaned> row; their usage also folds into the totals above.
    orphaned = _collect_orphaned(
        tool_dir,
        projects,
        prices,
        totals_by_model,
        totals_by_day_by_model,
        totals_by_day_by_source,
        from_iso=parsed.from_iso,
        until_iso=parsed.until_iso,
        scan_cache=scan_cache,
    )

    if not rows and orphaned is None:
        print("usage: no LiteLLM logs found for any registered project.", file=sys.stderr)
        return 0

    print(
        render(
            rows,
            totals_by_day_by_model,
            parsed.from_iso,
            parsed.until_iso,
            orphaned=orphaned,
        )
    )

    if parsed.verbose:
        breakdown = render_source_breakdown(
            totals_by_day_by_source, parsed.from_iso, parsed.until_iso
        )
        if breakdown:
            print()
            print(breakdown)

    # Footnote any successful requests whose usage was never recorded, so the
    # cost total is not silently understated (see _usage_unrecorded). The
    # per-model totals already aggregate every scanned request, orphaned included.
    unrecorded = sum(b.unrecorded for b in totals_by_model.values())
    if unrecorded:
        print(
            f"\nnote: {unrecorded} successful request(s) had unrecorded usage and "
            "contribute $0 to the totals above (response logged without a usage "
            "block). Cost is understated by their unknown amount.",
            file=sys.stderr,
        )
    return 0
