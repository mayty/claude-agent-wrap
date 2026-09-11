# This file has been edited with the assistance of an AI tool.
"""Constants for the providers domain subpackage."""

import re
from pathlib import Path

PROVIDERS_DIR = Path(__file__).parent

# Preferred base port for a provider's sidecar. The resolved port is scanned upward
# from here at cold start, so every provider can share one base. Chosen well above the
# usual ephemeral range and outside IANA's registered block to keep the window quiet.
DEFAULT_SIDECAR_PORT = 48620

# Matches context-length suffixes like [1m], [128k], [32k], [8k] on model names.
# Used by Provider.compute_cost to strip these when matching against pricing keys.
MODEL_CONTEXT_SUFFIX_RE = re.compile(r"\[(?:1m|128k|32k|8k)\]$", re.IGNORECASE)

# An unmatched model's worst-case cost (priced against the most expensive known
# tier) below this threshold is reported as a known $0 rather than unknown.
UNKNOWN_MODEL_COST_THRESHOLD_USD = 0.01

# How long a scraped pricing table stays usable. A week: published prices change on the
# order of months, and a provider's pricing page is not worth a round trip per run.
PRICING_CACHE_TTL_SECONDS = 7 * 24 * 3600

# HTTP timeout when fetching a pricing page. Short on purpose -- a slow page must not
# hold up `agent stats`, which falls back to the cached table.
PRICING_FETCH_TIMEOUT = 15

# Filename of a provider's cached pricing table, under its own state directory.
PRICING_CACHE_FILENAME = "pricing.json"
