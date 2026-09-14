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
DEFAULT_REGION_LABEL = "US East (N. Virginia)"

# Two known column schemas on the AWS Bedrock pricing page, picked by key
# count per row. The newest models (e.g. Opus 4.7) drop the batch columns.
PRICING_SCHEMAS = {
    7: ("in", "out", "in_batch", "out_batch", "cw_5m", "cw_1h", "cr"),
    5: ("in", "out", "cw_5m", "cw_1h", "cr"),
}

# Read from a row's or heading's *text*, once BeautifulSoup has taken the markup away.
MODEL_NAME_RE = re.compile(r"Claude\s+([A-Za-z]+)\s+(\d+(?:\.\d+)*)")
MODEL_KEY_RE = re.compile(
    r"priceOf!bedrockfoundationmodels/bedrockfoundationmodels!"
    r"([A-Za-z0-9_-]+)"
)
SECTION_HEADING_RE = re.compile(
    r"^(Global Cross-region Inference|Geo and In-region Cross-region Inference)$",
    re.IGNORECASE,
)

# The two inference tiers a heading can switch the walk into, and their precedence: a
# model listed under both is priced from the geo section.
GEO_SECTION = "geo"
GLOBAL_SECTION = "global"
SECTION_RANK = {GLOBAL_SECTION: 0, GEO_SECTION: 1}

# The page embeds part of its own markup as JSON, so these arrive escaped rather than as
# HTML entities -- BeautifulSoup would leave them alone, and the tags have to be real
# before it parses. Not the same job as ``html.unescape``, which handles the rest.
JSON_ESCAPED_MARKUP = (("\\u003c", "<"), ("\\u003e", ">"), ('\\"', '"'))
