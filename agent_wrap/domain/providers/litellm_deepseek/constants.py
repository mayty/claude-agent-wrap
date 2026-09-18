# This file has been created with the assistance of an AI tool.
"""Constants for the litellm_deepseek provider subpackage."""

import re

#: Official DeepSeek pricing page URL.
PRICING_PAGE_URL = "https://api-docs.deepseek.com/quick_start/pricing"

#: Minimum number of model entries expected in the pricing table.
MIN_MODEL_COUNT = 2

#: Weekdays DeepSeek's peak-hour rates apply to, as ``datetime.weekday()`` values
#: (0=Monday ... 6=Sunday). Peak hours are billed at peak rate Monday through
#: Friday only; weekends are off-peak all day. Hardcoded rather than scraped —
#: the footnote's "Monday through Friday" is stable, unlike the hour ranges.
PEAK_WEEKDAYS = frozenset({0, 1, 2, 3, 4})

# Patterns read from the page's *text*, once BeautifulSoup has taken the markup away.
# A trailing "(…)" on a model-name cell is a footnote marker, not part of the name.
FOOTNOTE_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
PEAK_HOURS_RE = re.compile(r"Peak hours are\s+([^.]*?)\s*UTC")
TIME_RANGE_RE = re.compile(r"(\d{1,2}):\d{2}\s*-\s*(\d{1,2}):\d{2}")
