# This file has been edited with the assistance of an AI tool.
"""LiteLLM Bedrock provider — routes Claude Code through AWS Bedrock."""

from __future__ import annotations

import gzip
import html
import json
import re
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pathlib import Path

from agent_wrap.providers.litellm_common import LiteLLMProvider

# Pricing source: the AWS Bedrock pricing page renders per-model cells as
# `{priceOf!bedrockfoundationmodels/bedrockfoundationmodels!<KEY>}` placeholders;
# at runtime the real numbers are pulled from `bedrockfoundationmodels.json`
# keyed by region. We do the same join offline and cache the resolved table.
_PRICING_PAGE_URL = "https://aws.amazon.com/bedrock/pricing/"
_PRICING_DATA_URL = (
    "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/"
    "bedrockfoundationmodels/USD/current/bedrockfoundationmodels.json"
)
_PRICING_CACHE_TTL_SECONDS = 7 * 24 * 3600
_PRICING_FETCH_TIMEOUT = 15
_DEFAULT_REGION_LABEL = "US East (N. Virginia)"

# Two known column schemas on the AWS Bedrock pricing page, picked by key
# count per row. The newest models (e.g. Opus 4.7) drop the batch columns.
_PRICING_SCHEMAS = {
    7: ("in", "out", "in_batch", "out_batch", "cw_5m", "cw_1h", "cr"),
    5: ("in", "out", "cw_5m", "cw_1h", "cr"),
}


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "User-Agent": "agent-wrap/agent_usage",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=_PRICING_FETCH_TIMEOUT) as resp:  # noqa: S310
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def _scrape_model_keys(page_html: str) -> dict[str, tuple[tuple[str, ...], list[str]]]:
    src = html.unescape(page_html).replace("\\u003c", "<").replace("\\u003e", ">")
    src = src.replace('\\"', '"')

    row_re = re.compile(r"<tr[^>]*>(?P<row>.*?</tr>)", re.DOTALL)
    name_re = re.compile(r"Claude\s+([A-Za-z]+)\s+(\d+(?:\.\d+)*)")
    key_re = re.compile(
        r"priceOf!bedrockfoundationmodels/bedrockfoundationmodels!"
        r"([A-Za-z0-9_-]+)"
    )
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

    rank = {"geo": 1, "global": 0}
    out: dict[str, tuple[int, tuple[str, ...], list[str]]] = {}
    for m in row_re.finditer(src):
        row = m.group("row")
        nm = name_re.search(row)
        if not nm:
            continue
        keys = key_re.findall(row)
        schema = _PRICING_SCHEMAS.get(len(keys))
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
    page_html: str, data_json: dict[str, Any], region_label: str
) -> dict[str, dict[str, float]]:
    region = data_json.get("regions", {}).get(region_label) or {}
    keys_by_model = _scrape_model_keys(page_html)

    table: dict[str, dict[str, float]] = {}
    for canonical, (schema, keys) in keys_by_model.items():
        cols = dict(zip(schema, keys, strict=True))
        try:
            row = {
                "in": float(region[cols["in"]]["price"]),
                "out": float(region[cols["out"]]["price"]),
                "cw_5m": float(region[cols["cw_5m"]]["price"]),
                "cw_1h": float(region[cols["cw_1h"]]["price"]),
                "cr": float(region[cols["cr"]]["price"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        table[canonical] = row
    return table


def _load_prices(
    cache_path: Path,
    region_label: str = _DEFAULT_REGION_LABEL,
    *,
    refresh: bool = False,
) -> dict[str, dict[str, float]]:
    cached: dict[str, Any] | None = None
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None

    fresh_enough = (
        cached is not None
        and cached.get("region") == region_label
        and isinstance(cached.get("fetched_at"), (int, float))
        and (time.time() - cached["fetched_at"]) < _PRICING_CACHE_TTL_SECONDS
    )

    if fresh_enough and not refresh and cached is not None:
        return cached.get("prices") or {}

    try:
        page = _http_get(_PRICING_PAGE_URL).decode("utf-8", errors="replace")
        data = json.loads(_http_get(_PRICING_DATA_URL))
        prices = _build_pricing_table(page, data, region_label)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        if cached:
            return cached.get("prices") or {}
        return {}

    if not prices:
        if cached:
            return cached.get("prices") or {}
        return {}

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


class BedrockProvider(LiteLLMProvider):
    name = "litellm-bedrock"
    image: ClassVar[str] = (
        "ghcr.io/berriai/litellm:v1.83.14-stable"
        "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
    )
    master_key_prefix: ClassVar[str] = "sk-aw-"
    secret_description: ClassVar[str] = "AWS Bedrock Bearer Token"  # noqa: S105

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": secrets.get("_secret_key", ""),
            "AWS_REGION_NAME": "us-east-1",
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": master_key,
            "ANTHROPIC_BEDROCK_BASE_URL": f"{base_url}/bedrock",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-east-1",
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []

    def get_pricing(self) -> dict[str, dict[str, float]]:
        """Return the cached AWS Bedrock pricing table for this provider."""
        cache_path = self._state_dir() / "pricing.json"
        return _load_prices(cache_path)
