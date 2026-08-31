# This file has been created with the assistance of an AI tool.
"""Data models for the config domain."""

from dataclasses import dataclass


@dataclass
class Entry:
    """Intermediate representation used during project-registry compression."""

    compressed: str
    first_original: str
    last_original: str
