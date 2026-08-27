# This file has been created with the assistance of an AI tool.
"""Constants for the startup domain."""

from __future__ import annotations

# Interpreter used for a startup script that carries no usable shebang. Deliberately
# ``/bin/sh`` rather than bash: a script relying on bash features declares
# ``#!/usr/bin/env bash`` and gets it.
DEFAULT_STARTUP_RUNNER = "/bin/sh"

# Bytes of the script read while looking for a shebang. A shebang lives on the first
# line, and a script with a very long one is not one we want to guess at.
SHEBANG_PROBE_BYTES = 512
