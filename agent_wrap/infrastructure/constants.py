# This file has been created with the assistance of an AI tool.
"""Constants for the infrastructure layer."""

import re
from enum import StrEnum
from pathlib import Path
from typing import Final

# This package's own directory. Each database's subpackage sits directly under it, named
# after its ``Databases`` member, which is what makes its migrations path derivable.
# Resolved from __file__ because the project is never installed -- bin/agent puts the
# checkout on PYTHONPATH, so the source tree is always the runtime tree.
INFRASTRUCTURE_DIR = Path(__file__).parent


class Databases(StrEnum):
    """
    Every database this layer owns.

    A member's value is three things at once: the name of its subpackage under
    ``INFRASTRUCTURE_DIR`` (hence of its ``migrations/`` directory), the stem of its
    file in the database directory, and the prefix of its backup filenames. Adding a
    database is adding a member here plus a subpackage named after it -- no path
    constant of its own.
    """

    PROJECTS = "projects"


# Extension of every database file, appended to the ``Databases`` member's value.
DB_FILE_SUFFIX = ".db"

# Sub-directory of AGENT_LAUNCHES_DIR holding every database file. Named here, but
# composed into a path only at the DI composition root (``agent_wrap/containers.py``)
# -- ConnectionFactory itself takes fully-formed paths so a test can point it anywhere.
DB_DIRNAME = "db"

# Sub-directory of the database directory holding pre-migration backups. Kept forever:
# a backup is the only recovery path for a migration that corrupts data, and pruning
# would silently discard the one that matters.
BACKUPS_DIRNAME = "backups"

# Migration filenames: NNNN_<slug>.sql, ordered by the integer prefix.
MIGRATION_RE = re.compile(r"^(\d+)_[a-z0-9_]+\.sql$")

# Directory name, inside each database's subpackage, holding its .sql migrations.
MIGRATIONS_DIRNAME = "migrations"

# How long a blocked writer waits for the lock before raising. Generous because the
# competing writers are `agent run` launches and the logs daemon, both of which hold
# the database for microseconds -- a timeout here means something is genuinely wedged.
BUSY_TIMEOUT_MS = 5000

# PRAGMAs that persist in the database file itself, so they are applied once when the
# factory opens the database rather than on every connection. WAL is what lets the logs
# daemon read while a launch writes, instead of blocking on it.
DATABASE_PRAGMAS: Final[tuple[str, ...]] = ("PRAGMA journal_mode = WAL",)

# PRAGMAs that live on the connection and must therefore be re-applied to each one.
# NORMAL synchronous is the documented safe pairing with WAL -- a power loss can lose
# the last commit but never corrupt the file, and this is a convenience index rather
# than a ledger.
CONNECTION_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}",
)

# Timestamp format for backup filenames: compact UTC, sorts lexicographically.
BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
