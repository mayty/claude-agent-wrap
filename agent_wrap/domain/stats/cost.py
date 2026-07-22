# This file has been edited with the assistance of an AI tool.
"""Usage-source classification for the stats command."""

from __future__ import annotations

from typing import Any


def usage_source(rec: dict[str, Any]) -> str:
    """
    Classify how a success record's usage was obtained, for the verbose breakdown.

    Mirrors the three outcomes the callback's ``_usable_response`` stamps onto a
    record's ``response`` (see ``providers/litellm_common/callback.py``):
      * ``"native"`` — a parsed response dict with no ``_usage_source`` key (usage
        came straight from the response);
      * ``"standard_logging_object"`` — dict tagged with that source (usage was
        recovered from LiteLLM's standard logging object fallback);
      * ``"unrecoverable"`` — dict tagged ``"unrecoverable"``, or a bare legacy
        ``"<Response ...>"`` string; no usable usage at all.
    """
    response = rec.get("response")
    if isinstance(response, str):
        return "unrecoverable"
    if isinstance(response, dict):
        src = response.get("_usage_source")
        if src in ("standard_logging_object", "unrecoverable"):
            return src
        return "native"
    return "unrecoverable"
