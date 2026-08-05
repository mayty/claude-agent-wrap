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
