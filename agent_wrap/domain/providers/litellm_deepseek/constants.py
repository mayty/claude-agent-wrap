# This file has been created with the assistance of an AI tool.
"""Constants for the litellm_deepseek provider subpackage."""

#: Official DeepSeek pricing page URL.
PRICING_PAGE_URL = "https://api-docs.deepseek.com/quick_start/pricing"

#: Cache TTL for scraped pricing data (7 days in seconds).
PRICING_CACHE_TTL_SECONDS = 7 * 24 * 3600

#: HTTP request timeout in seconds when fetching the pricing page.
PRICING_FETCH_TIMEOUT = 15

#: Minimum number of model entries expected in the pricing table.
MIN_MODEL_COUNT = 2
