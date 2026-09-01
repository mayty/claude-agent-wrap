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

#: Weekdays DeepSeek's peak-hour rates apply to, as ``datetime.weekday()`` values
#: (0=Monday ... 6=Sunday). Peak hours are billed at peak rate Monday through
#: Friday only; weekends are off-peak all day. Hardcoded rather than scraped —
#: the footnote's "Monday through Friday" is stable, unlike the hour ranges.
PEAK_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
