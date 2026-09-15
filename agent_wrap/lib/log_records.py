# This file has been edited with the assistance of an AI tool.
"""
Pure classification helpers over one raw log record.

In ``lib/`` rather than a domain subpackage because both the ingester and the stats
fold need it, and a runtime import between two domain subpackages is a layering break
(EA001). Nothing here touches the filesystem or any service.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def usage_source(rec: Mapping[str, Any]) -> str:
    """
    Classify how a success record's usage was obtained.

    Stored per request so ``"unrecoverable"`` can be counted: those requests contribute
    $0 to the totals, and the stats footnote says so rather than letting them read as
    free. Mirrors the three outcomes the callback's ``_usable_response`` stamps onto a
    record's ``response`` (see ``providers/litellm_runtime/callback.py``):
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
