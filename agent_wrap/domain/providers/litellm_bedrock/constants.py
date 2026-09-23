# This file has been created with the assistance of an AI tool.
"""Constants for the LiteLLM Bedrock provider."""

import re

# Pricing source: the AWS Bedrock pricing page ships each pricing table as HTML inside a
# `data-pricing-markup` attribute, with per-model cells as
# `{priceOf!bedrockfoundationmodels/bedrockfoundationmodels!<KEY>}` placeholders;
# at runtime the real numbers are pulled from `bedrockfoundationmodels.json`
# keyed by region. We do the same join offline and cache the resolved table.
PRICING_MARKUP_ATTR = "data-pricing-markup"
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

# Matched against a row's first cell, whole, so "Claude Sonnet 4.6 - Long Context" and
# "Claude Mythos Preview" claim no key. The page writes both "Claude Haiku 4.5" and
# "Claude 4.5 Haiku", and footnotes a name with trailing asterisks.
MODEL_NAME_RE = re.compile(
    r"^Claude\s+(?:"
    r"(?P<family>[A-Za-z]+)\s+(?P<version>\d+(?:\.\d+)*)"
    r"|(?P<version_first>\d+(?:\.\d+)*)\s+(?P<family_last>[A-Za-z]+)"
    r")\**$"
)
MODEL_KEY_RE = re.compile(
    r"priceOf!bedrockfoundationmodels/bedrockfoundationmodels!"
    r"([A-Za-z0-9_-]+)"
)
SECTION_HEADING_RE = re.compile(
    r"^(Global Cross[- ]region Inference|Geo and In-region Cross[- ]region Inference)$",
    re.IGNORECASE,
)

# The two inference tiers a heading can switch the walk into, and their precedence: a
# model listed under both is priced from the geo section.
GEO_SECTION = "geo"
GLOBAL_SECTION = "global"
SECTION_RANK = {GLOBAL_SECTION: 0, GEO_SECTION: 1}

# Markup the page embeds as JSON arrives with these escaped rather than as HTML entities
# -- BeautifulSoup would leave them alone, and the tags have to be real before it parses.
# Entities are BeautifulSoup's job: decoding them over the raw page instead turns the
# ``&quot;`` inside other attributes into quotes that end those attributes early.
JSON_ESCAPED_MARKUP = (("\\u003c", "<"), ("\\u003e", ">"), ('\\"', '"'))
