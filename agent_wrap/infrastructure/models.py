# This file has been created with the assistance of an AI tool.
"""Data models for the infrastructure layer."""

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path


class MigrationFile(NamedTuple):
    version: int
    path: Path
