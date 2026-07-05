# This file has been created with the assistance of an AI tool.
"""Constants for the pricing domain subpackage."""

import re

MODEL_FAMILY_RE_V_FIRST = re.compile(
    r"claude[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)[-\s.]*(?P<tier>[a-z]+)",
    re.IGNORECASE,
)
MODEL_FAMILY_RE_T_FIRST = re.compile(
    r"claude[-\s.]*(?P<tier>[a-z]+)[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)",
    re.IGNORECASE,
)
DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
