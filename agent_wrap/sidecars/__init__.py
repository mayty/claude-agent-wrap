# This file has been created with the assistance of an AI tool.
"""
Sidecar abstraction.

A *sidecar* is a shared, host-wide helper container that an agent run depends on
(today: the LiteLLM proxy; later: a decision-maker). The runner collects a list of
sidecars, holds one shared lock around them, ensures each before launching the agent,
and releases each on exit.

`Sidecar` (``base.py``) is the narrow interface every sidecar implements — pure
container mechanics. `SidecarTracker` (``tracker.py``) is the one common per-run
coordinator the runner consults under that lock: the activity heartbeat, the live
agent count, and the single stop decision.
"""

from agent_wrap.sidecars.base import Sidecar
from agent_wrap.sidecars.tracker import ActivityRecord, SidecarTracker

__all__ = ["ActivityRecord", "Sidecar", "SidecarTracker"]
