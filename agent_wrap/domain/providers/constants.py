# This file has been edited with the assistance of an AI tool.
"""Constants for the providers domain subpackage."""

import re
from pathlib import Path

PROVIDERS_DIR = Path(__file__).parent

# Matches context-length suffixes like [1m], [128k], [32k], [8k] on model names.
# Used by Provider.compute_cost to strip these when matching against pricing keys.
MODEL_CONTEXT_SUFFIX_RE = re.compile(r"\[(?:1m|128k|32k|8k)\]$", re.IGNORECASE)
