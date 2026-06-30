# This file has been created with the assistance of an AI tool.
"""
Sidecar abstraction.

A *sidecar* is a shared, host-wide helper container that an agent run depends on
(today: the LiteLLM proxy; later: a decision-maker). The runner collects a list of
sidecars, holds one shared lock around them, ensures each before launching the agent,
and releases each on exit.

`Sidecar` (``base.py``) is the narrow interface every sidecar implements — pure
container mechanics. `SidecarTracker` (``tracker.py``) is the one common per-run
coordinator the runner consults under that lock: the lock-held registries of
starting and running agents that drive the single teardown decision.
"""

from agent_wrap.sidecars.base import Sidecar
from agent_wrap.sidecars.telegram import TelegramSidecar, TelegramSidecarConfig
from agent_wrap.sidecars.tracker import SidecarTracker

__all__ = ["Sidecar", "SidecarTracker", "TelegramSidecar", "TelegramSidecarConfig"]
