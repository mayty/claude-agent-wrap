# This file has been created with the assistance of an AI tool.
"""Model-id normalization shared by the usage-stats subcommands."""

from __future__ import annotations

import re

_MODEL_FAMILY_RE_V_FIRST = re.compile(
    r"claude[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)[-\s.]*(?P<tier>opus|sonnet|haiku)",
    re.IGNORECASE,
)
_MODEL_FAMILY_RE_T_FIRST = re.compile(
    r"claude[-\s.]*(?P<tier>opus|sonnet|haiku)[-\s.]*(?P<ver>\d+(?:[.\-]\d+)*)",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def normalize_model(model: str) -> str | None:
    """
    Return a canonical 'claude-<tier>-<ver>' key for a session model id.

    Handles the various forms session JSONLs surface:
      claude-opus-4-7
      claude-sonnet-4-5-20250929          (date-stamped snapshot)
      anthropic.claude-opus-4-7-v1:0      (Bedrock model id form)
      Claude Opus 4.7                     (display name)

    Returns None if `model` doesn't look like a Claude release.
    """
    if not model:
        return None
    bare = _DATE_SUFFIX_RE.sub("", model)
    m = _MODEL_FAMILY_RE_T_FIRST.search(bare) or _MODEL_FAMILY_RE_V_FIRST.search(bare)
    if not m:
        return None
    tier = m.group("tier").lower()
    ver = m.group("ver").replace(".", "-")
    return f"claude-{tier}-{ver}"
