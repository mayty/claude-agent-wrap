# This file has been created with the assistance of an AI tool.
"""Constants for the LiteLLM Bedrock provider."""

import re

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

# Compiled regexes for scraping the Bedrock pricing page.
ROW_RE = re.compile(r"<tr[^>]*>(?P<row>.*?</tr>)", re.DOTALL)
MODEL_NAME_RE = re.compile(r"Claude\s+([A-Za-z]+)\s+(\d+(?:\.\d+)*)")
MODEL_KEY_RE = re.compile(
    r"priceOf!bedrockfoundationmodels/bedrockfoundationmodels!"
    r"([A-Za-z0-9_-]+)"
)
SECTION_RE = re.compile(
    r"<h2[^>]*>\s*(Global Cross-region Inference"
    r"|Geo and In-region Cross-region Inference)\s*</h2>",
    re.IGNORECASE,
)
