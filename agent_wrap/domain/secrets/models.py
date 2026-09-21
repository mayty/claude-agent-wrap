# This file has been created with the assistance of an AI tool.
"""Data models for the secrets domain."""

from typing import NamedTuple


class SecretEntry(NamedTuple):
    """
    One secret's presence and, when it is present, enough of it to recognize by eye.

    ``length`` and ``hint`` are what tell a doubled paste from a correct value without
    printing the secret; both stay at their defaults for an absent key.
    """

    present: bool
    length: int = 0
    hint: str = ""


class SecretsCheckReport(NamedTuple):
    """
    The presence of every secret a sidecar requires, plus the overall verdict.

    ``declares_none`` distinguishes a sidecar with no required secrets from one whose
    secrets are all present — both have no missing keys, but only the latter is a
    meaningful "all OK".
    """

    #: Namespaced key -> what is known about it, in declaration order.
    entries: dict[str, SecretEntry]
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
