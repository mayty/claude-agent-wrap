# This file has been created with the assistance of an AI tool.
"""Constants for the build domain."""

# Seconds a project startup script may run when ``# agent-enable-startup:`` is given a
# plain boolean. Deliberately short: the script runs while holding the host-global
# sidecar lock, so every concurrently launching agent waits behind it. A project that
# genuinely needs longer states its own budget (``# agent-enable-startup: 45``).
DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0

# Directive values that mean "on, with the default timeout" and "off".
STARTUP_TRUTHY_WORDS = frozenset({"true", "yes", "on"})
STARTUP_FALSY_WORDS = frozenset({"false", "no", "off"})
