# This file has been created with the assistance of an AI tool.
"""Data models for the secrets domain."""

from __future__ import annotations

from typing import NamedTuple


class SecretsCheckReport(NamedTuple):
    """
    The presence of every secret a sidecar requires, plus the overall verdict.

    ``declares_none`` distinguishes a sidecar with no required secrets from one whose
    secrets are all present — both have no missing keys, but only the latter is a
    meaningful "all OK".
    """

    #: Namespaced key -> whether it is present in the store, in declaration order.
    entries: dict[str, bool]
    #: Whether every required secret is present. True when none are required.
    all_present: bool
    #: Whether the sidecar requires no secrets at all.
    declares_none: bool


class SecretsSetResult(NamedTuple):
    """
    The outcome of prompting for a sidecar's secrets.

    *error* is set when the secrets could not be prompted for at all (no TTY), which
    a caller reports as a failure rather than as "nothing to set".
    """

    keys_set: list[str]
    error: str | None = None
