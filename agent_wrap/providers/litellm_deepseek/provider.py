# This file has been edited with the assistance of an AI tool.
"""LiteLLM DeepSeek provider — routes Claude Code through DeepSeek provider."""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import ClassVar

from agent_wrap.providers.litellm_common import LiteLLMProvider

# ---------------------------------------------------------------------------
# Dynamic pricing: scrape the official DeepSeek pricing page and cache for
# 7 days, following the same pattern as litellm_bedrock.
# ---------------------------------------------------------------------------
_PRICING_PAGE_URL = "https://api-docs.deepseek.com/quick_start/pricing"
_PRICING_CACHE_TTL_SECONDS = 7 * 24 * 3600
_PRICING_FETCH_TIMEOUT = 15


def _http_get(url: str) -> bytes:
    """Fetch *url* and return its raw bytes."""
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


_MIN_MODEL_COUNT = 2


def _extract_dollar_amounts(text: str) -> list[float]:
    """Return all dollar-denominated numbers found in *text*."""
    return [float(m) for m in re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", text)]


def _clean_model_name(cell_html: str) -> str:
    """Strip HTML tags and footnote markers from a model-name <td>."""
    name = re.sub(r"<[^>]+>", "", cell_html).strip()
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _parse_model_names(header_row: str) -> list[str]:
    """Extract canonical model names from the table's header row."""
    cells = re.findall(r"<td[^>]*>(.*?)</td>", header_row, re.DOTALL)
    models: list[str] = []
    for cell in cells[1:]:  # skip the "MODEL" label cell
        name = _clean_model_name(cell)
        if name:
            models.append(name)
    return models


def _fill_price_column(
    prices: dict[str, dict[str, float]],
    models: list[str],
    row_html: str,
    label: str,
    field: str,
) -> None:
    """Extract dollar amounts from *row_html* and assign to *field* per model."""
    if label not in row_html:
        return
    amounts = _extract_dollar_amounts(row_html)
    for i, model in enumerate(models):
        if i < len(amounts):
            prices[model][field] = amounts[i]


def _parse_pricing_page(page_html: str) -> dict[str, dict[str, float]]:
    """Parse the DeepSeek pricing page HTML into a flat pricing table."""
    table_m = re.search(r"<table[^>]*>(.*?)</table>", page_html, re.DOTALL)
    if not table_m:
        return {}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(1), re.DOTALL)
    if len(rows) < _MIN_MODEL_COUNT:
        return {}

    models = _parse_model_names(rows[0])
    if len(models) < _MIN_MODEL_COUNT:
        return {}

    prices: dict[str, dict[str, float]] = {
        m: {"in": 0.0, "out": 0.0, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.0} for m in models
    }

    for row in rows:
        _fill_price_column(prices, models, row, "CACHE HIT", "cr")
        _fill_price_column(prices, models, row, "CACHE MISS", "in")
        _fill_price_column(prices, models, row, "OUTPUT TOKENS", "out")

    # Sanity check: every model must have a positive input price
    for model, row_data in list(prices.items()):
        if row_data["in"] <= 0:
            del prices[model]
    return prices


def _load_prices(cache_path: Path) -> dict[str, dict[str, float]]:
    """Return cached or freshly-scraped DeepSeek pricing."""
    cached: dict | None = None
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None

    fresh_enough = (
        cached is not None
        and isinstance(cached.get("fetched_at"), (int, float))
        and (time.time() - cached["fetched_at"]) < _PRICING_CACHE_TTL_SECONDS
    )

    if cached is not None and fresh_enough:
        return cached.get("prices") or {}

    try:
        page = _http_get(_PRICING_PAGE_URL).decode("utf-8", errors="replace")
        prices = _parse_pricing_page(page)
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        if cached is not None:
            return cached.get("prices") or {}
        return {}

    if not prices:
        if cached is not None:
            return cached.get("prices") or {}
        return {}

    # Persist the freshly-scraped table
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"fetched_at": time.time(), "prices": prices},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    return prices


class DeepSeekProvider(LiteLLMProvider):
    name = "litellm-deepseek"
    image: ClassVar[str] = (
        "ghcr.io/berriai/litellm:v1.83.14-stable"
        "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
    )
    master_key_prefix: ClassVar[str] = "sk-ds-"

    def read_secret_key(self, secrets: dict) -> str:
        key = secrets.get("DeepSeekAPIKey", "")
        if not key:
            msg = "litellm-sidecar: .DeepSeekAPIKey missing or empty in ~/claude_keys.json"
            raise SystemExit(msg)
        return key

    def get_sidecar_env(self, secrets: dict) -> dict[str, str]:
        return {
            "DEEPSEEK_API_KEY": secrets.get("_secret_key", ""),
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []

    def get_pricing(self) -> dict[str, dict[str, float]]:
        """Return the cached DeepSeek pricing table, scraping if stale."""
        cache_path = self._state_dir() / "pricing.json"
        return _load_prices(cache_path)

    # --- API key auto-approval ---

    def ensure(
        self,
        *,
        use_host_net: bool,
        instance_id: str,
        agent_network: str | None,
    ) -> None:
        super().ensure(
            use_host_net=use_host_net,
            instance_id=instance_id,
            agent_network=agent_network,
        )
        self._approve_master_key(self._master_key)

    def release(self, instance_id: str) -> None:
        super().release(instance_id)
        self._unapprove_master_key(self._master_key)

    @staticmethod
    def _api_key_approval_id(key: str) -> str:
        """Return the identifier Claude Code uses to track key approval (last 20 chars)."""
        return key[-20:]

    def _claude_json_path(self) -> Path:
        """Resolve the global .claude.json file path."""
        # litellm_deepseek/provider.py -> litellm_deepseek/ -> providers/ -> agent_wrap/ -> repo root
        tool_dir = Path(__file__).resolve().parent.parent.parent.parent
        return tool_dir / ".claude_config" / ".claude.json"

    def _load_claude_json(self) -> dict | None:
        """Load .claude.json, returning {} if missing or None on malformed JSON."""
        path = self._claude_json_path()
        if not path.exists():
            return {}
        try:
            text = path.read_text()
            if not text.strip():
                return {}
            return json.loads(text)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_claude_json(self, data: dict) -> None:
        """Atomically write .claude.json."""
        path = self._claude_json_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)

    def _approve_master_key(self, key: str) -> None:
        """Add the current master key's approval ID to .claude.json."""
        data = self._load_claude_json()
        if data is None:
            return
        approval_id = self._api_key_approval_id(key)
        approved = data.setdefault("customApiKeyResponses", {}).setdefault("approved", [])
        if approval_id not in approved:
            approved.append(approval_id)
            data.setdefault("customApiKeyResponses", {})["rejected"] = data.get(
                "customApiKeyResponses", {}
            ).get("rejected", [])
            self._save_claude_json(data)

    def _unapprove_master_key(self, key: str) -> None:
        """Remove the current master key's approval ID from .claude.json."""
        data = self._load_claude_json()
        if data is None:
            return
        approval_id = self._api_key_approval_id(key)
        approved = data.get("customApiKeyResponses", {}).get("approved", [])
        if approval_id in approved:
            approved.remove(approval_id)
            self._save_claude_json(data)
